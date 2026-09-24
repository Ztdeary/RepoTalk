from fastapi import APIRouter,Depends # 用于定义路由和依赖注入
from pydantic import BaseModel # 用于定义请求体和响应体的模型
from langchain_openai import OpenAIEmbeddings # 用于向量化
from app.core.config import settings # 用于获取配置参数
from langchain_postgres import PGVector # 用于向量数据库
from app.rag.pipeline import generate_answer # 缓存命中/未命中统一走它生成回答
from app.core.cache import get_cached_docs,set_cached_docs # 检索缓存读写
from fastapi import HTTPException
# 切不出代码块时要返回明确的 4xx 错误，而不是静默成功。FastAPI 抛出它就会自动转成对应的 HTTP 错误响应。
from sqlalchemy import select # 查询repos表
from sqlalchemy.ext.asyncio import AsyncSession # 类型标注用
from app.core.db import get_session # FastAPI 依赖,每个请求一个会话
from app.models import Repo,RepoStatus # Repo模型+状态常量
from app.rag.hybrid_retriever import _bm25_cache_hit # BM25三级查找函数
from fastapi.responses import StreamingResponse # 流式响应类:yield一条就立刻推一条,不攒齐再发
from app.rag.hybrid_retriever import hybrid_search # 混合检索函数,流式路由要在生成器里单独调它
from app.rag.pipeline import stream_answer_hybrid # 刚在pipeline.py里写的流式生成函数
from app.core.security import get_current_user # 从依赖注入里获取当前用户

import json # 把每条SSE消息的业务字典序列化成JSON字符串(SSE协议只传文本)
import asyncio # 用asyncio.to_thread把同步检索丢线程池+asyncio.wait实现检索期心跳

router = APIRouter(
    dependencies=[Depends(get_current_user)]
    # 保护/chat与/chat/stream:这两个接口会真实消耗DeepSeek/硅基流动的API额度, 
    # 是整个后端最"烧钱"的接口,必须验token 
    # 对/chat/stream特别注意:门禁在流开始之前执行, 
    # 401能作为正常HTTP错误响应返回;一旦流开始了再失败,客户端只能看到"流断了"
) # 这个router会被main.py include

# 进程级单例:向量库连接+Embeddings 
# 为什么放模块级? 
# FastAPI每个请求都可能用到,构造一次就行,不用每次请求都新建。
_embeddings=OpenAIEmbeddings(
    model=settings.embedding_model,
    base_url=settings.embedding_base_url,
    api_key=settings.embedding_api_key,
    timeout=30, max_retries=1
)

def _get_store(collection:str)->PGVector:
    """按collection名构造PGVector连接,只读,不带pre_delete_collection。
    每次调用都new一个实例,不跨请求共享store对象,避免阻塞事件循环。"""
    return PGVector(
        embeddings=_embeddings,
        collection_name=collection,
        connection=settings.database_url,
    )

# Pydantic模型层:定义请求体的结构、校验规则 
# 为什么用BaseModel?
# FastAPI自动把它转成JSON Schema,Swagger UI直接生成表单
class ChatReq(BaseModel):
    """/chat 请求体。
    repo_id与collection二选一优先repo_id,因为repo_id能在repos表查到collection名,
    而collection名反过来不一定能对应到活跃的repo记录。"""
    repo_id:int|None=None # 推荐:指定仓库id,自动查对应的collection
    collection:str|None=None # 兼容旧版:直接指定collection名
    question:str # 用户的问题,必填

# /chat 路由:混合检索问答 
# BM25来源从_bm25_cache.get()改成_bm25_cache_hit(repo_id->collection) 
# 新增按repo_id查repos表解析collection的逻辑 
# 用async def+get_session依赖注入拿数据库会话
@router.post("/chat")
async def chat(req:ChatReq,session:AsyncSession=Depends(get_session)): # ... 会被 FastAPI 自动注入
    """混合检索 + 生成回答。
    collection解析:优先用repo_id查repos表,没给repo_id就直接用collection字段。"""
    # 1.确定要查哪个collection
    if req.repo_id is not None:
        # 给了repo_id->查repos表拿collection名
        repo=(
            await session.execute(select(Repo).where(Repo.id==req.repo_id))
        ).scalar_one_or_none()
        if repo is None:
            raise HTTPException(
                status_code=404,
                detail=f"repo_id={req.repo_id}不存在"
            )
        if repo.status!=RepoStatus.DONE:
            raise HTTPException(
                status_code=400,
                detail=f"repo_id={req.repo_id}还没解析完(status={repo.status}),请等done后再问"
            )
        collection=repo.collection
    elif req.collection is not None:
        collection=req.collection
    else:
        raise HTTPException(
            status_code=400,
            detail=f"必须提供repo_id或collection其中之一"
        )

    # 2.向量库连接
    store=_get_store(collection)

    # 3.BM25:三级查找(内存->Redis->None)
    bm25=_bm25_cache_hit(collection)
    if bm25 is None:
        # 三级全miss->这个collection从未被ingest过 
        # 错误信息要明确告诉用户是"还没解析"还是"解析失败了" 
        # 但此时我们没repos表信息(因为可能是旧版手工ingest的), 
        # 就给一个通用提示,让用户看repos列表排查
        raise HTTPException(
            status_code=400,
            detail=f"collection='{collection}'的BM25索引不存在,请先通过POST/repos发起解析"
        )

    # 4.读检索缓存:命中直接拿docs,未命中才跑完整混合检索
    docs=get_cached_docs(req.repo_id,collection,req.question)
    if docs is None:
        # 未命中:hybrid_search是同步阻塞(embedding+rerank都是网络IO),丢线程池
        docs=await asyncio.to_thread(hybrid_search,req.question,store,bm25)
        # 检索完立刻写缓存,下次同问题直接命中;写失败不影响本次回答
        set_cached_docs(req.repo_id,collection,req.question,docs)
    
    # 5.生成:无论docs来自缓存还是现查,生成这一步都要现场做 
    # generate_answer是同步阻塞(chain.invoke等LLM写完整篇),同样丢线程池
    loop=asyncio.get_running_loop()
    # loop:接收拿到的事件循环对象,后面要用它调度线程池 
    # get_running_loop():获取"当前正在运行的事件循环" 
    # async def路由跑起来时FastAPI已把它放进事件循环,这个函数把那个循环对象取出来
    return await loop.run_in_executor(
        None, # None=用事件循环默认线程池,省得自己管理线程池生命周期
        generate_answer, # 要扔进线程池执行的同步函数
        req.question,docs, # 传给generate_answer的两个位置参数
    )

async def _resolve_collection(req:ChatReq,session:AsyncSession)->str:
    """从请求里解析出要查的collection名。
    校验逻辑与/chat开头那段完全相同,抽成独立函数是为了让/chat/stream复用,
    避免同一段校验写两遍、以后改一处漏一处。
    旧/chat保持原样不动——它已通过验收,不为"好看"去动能跑的代码。
    注意:本函数抛出的HTTPException都发生在流式响应开始之前,
    所以FastAPI还能正常把它转成404/400响应;
    一旦流开始了再抛,客户端收到的只会是"流到一半断了",看不到状态码。"""
    if req.repo_id is not None:
        # 给了repo_id->查repos表拿collection名
        repo=(
            await session.execute(select(Repo).where(Repo.id==req.repo_id))
        ).scalar_one_or_none()
        if repo is None:
            raise HTTPException(
                status_code=404,
                detail=f"repo_id={req.repo_id}不存在"
            )
        if repo.status!=RepoStatus.DONE:
            # 仓库还在解析中,向量库里数据不全,此时问答结果必然不靠谱,直接拒绝
            raise HTTPException(
                status_code=400,
                detail=f"repo_id={req.repo_id}还没解析完(status={repo.status}),请等done后再问"
            )
        return repo.collection
        # 以上两处校验通过,返回collection名
    if req.collection is not None:
        # 兼容旧版:直接指定collection名
        return req.collection
    # 两个都没给,没法查,明确报400
    raise HTTPException(
        status_code=400,
        detail=f"必须提供repo_id或collection其中之一"
    )

@router.post("/chat/stream",description="SSE流式问答:先推引用来源,再逐token推回答")
async def chat_stream(req:ChatReq,session:AsyncSession=Depends(get_session)):
    """POST/chat/stream——流式版问答。
    与/chat的唯一区别是"输出方式":
    /chat等LLM写完整篇回答再一次返回JSON;
    本接口检索完立刻推sources,然后LLM每吐一个词就推一条SSE消息。
    检索逻辑、prompt、LLM参数全部复用,只变传输,不变智能。"""
    # ===== 流开始前:三项校验,失败抛HTTP异常 =====
    collection=await _resolve_collection(req,session) # 校验1+2:repo存在性+解析完成状态
    store=_get_store(collection) # 向量库连接(只读,不带pre_delete_collection)
    bm25=_bm25_cache_hit(collection) # 校验3:BM25索引三级查找(内存->Redis->None)
    if bm25 is None:
        raise HTTPException(
            status_code=400,
            detail=f"collection='{collection}'的BM25索引不存在,请先通过POST/repos发起解析"
        )
    
    # ===== 流开始:定义异步生成器 =====
    async def token_stream():
        """异步生成器:StreamingResponse会驱动它,
        每执行到一次yield,那一条数据立刻推给浏览器,然后函数暂停在yield处等下一段。""" 
        # 阶段1:先查缓存——命中就跳过整个检索阶段,连"检索中"状态都不用推
        docs=get_cached_docs(req.repo_id,collection,req.question)

        if docs is None:
            # 未命中:推"检索中"状态,让前端马上有反馈(显示"正在检索资料...") 
            # 顺带完成SSE开流,响应头立刻发出,连接正式建立
            yield f"data:{json.dumps({'type':'status','stage':'retrieving'},ensure_ascii=False)}\n\n"
            # ensure_ascii=False:中文保持UTF-8原文输出 
            # \n\n:SSE协议硬性规定,每条消息必须以两个换行结尾

            # 阶段2:执行检索(同步阻塞,丢线程池)+等待期心跳
            task=asyncio.create_task(
                asyncio.to_thread(hybrid_search,req.question,store,bm25)
                # hybrid_search里BM25打分+rerank的HTTP调用都是同步阻塞 
                # 直接在事件循环里跑会把整个FastAPI服务卡死
            )
    
            while True:
                # asyncio.wait:等一组任务,timeout秒内没完成也不报错,只是返回空done集合 
                # 我们要的就是这个特性:每10秒醒来一次,没检索完就发条心跳,检索完就跳出
                done,_pending=await asyncio.wait({task},timeout=10)
                # done的类型:set[asyncio.Task]——到点前已结束的任务
                # _pending的类型:set[asyncio.Task]——到点还没结束的任务
                # 两个集合加起来永远等于你传进去的那个集合
                # 这里就是{task}:要么task在done里,要么在_pending里
                if done:
                    break # 检索结束(无论成功失败),跳出心跳循环,下面统一处理结果
                yield ": ping\n\n" # 如果是_pending,说明检索还在,发条心跳保持连接
                # SSE协议:冒号开头的行是注释,浏览器自动忽略 
                # 但字节真实流过连接,防止Week 6的Nginx或中间代理因长时间无数据掐线 
                # 为什么这里要心跳而LLM阶段不要:检索最重的一环是rerank API调用,timeout=30秒, 
                # 最坏30秒无任何输出;LLM流式阶段每隔几十毫秒就有token流出,天然就是心跳
            try:
                docs=task.result()
                # task.result():拿任务的返回值。任务若是异常结束,这行会把异常重新抛出来 
                # 所以必须用try包住,不能让异常裸奔出生成器(那会导致流直接断掉,前端连原因都看不到)
                set_cached_docs(req.repo_id,collection,req.question,docs)
                # 检索成功:立刻写缓存,下次同问题跳过整个阶段2
            except Exception as e:
                yield f"data:{json.dumps({'type':'error','message':f'检索失败:{e}'},ensure_ascii=False)}\n\n"
                return # 生成器return=流正常结束,浏览器收到EOF

        # 阶段3:先推sources(引用来源列表)
        # 为什么sources在token之前:检索结果此刻已确定,不依赖LLM
        # 先推给前端可以立刻渲染引用卡片;万一后面LLM挂了,用户至少能看到查到了哪些资料
        sources=[
            {
                "file_path":d.metadata.get("file_path"), # 相对路径,前端跳转用
                "start_line":d.metadata.get("start_line"), # 起始行,前端定位用
                "end_line":d.metadata.get("end_line"), # 结束行,前端定位用
                "symbol":d.metadata.get("symbol"), # 函数/类名,前端展示用
                "content":d.page_content[:200], # 前200字摘要,预览用
            }
            for d in docs
        ] # 字段结构与answer_hybrid返回的source完全对齐,前端一套渲染逻辑吃两个接口
        yield f"data:{json.dumps({'type':'sources','sources':sources},ensure_ascii=False)}\n\n"
            
        # 阶段4:LLM流式生成,逐token转发 
        # stream_answer_hybrid是异步生成器,用async for消费: 
        # 它每yield一个文本片段,这里就立刻包成SSE消息推给前端 
        # 注意这里不需要线程池:chain.astream是原生异步实现,token从网卡直达事件循环,零缓冲
        try:
            async for token in stream_answer_hybrid(req.question,docs):
                yield f"data:{json.dumps({'type':'token','content':token},ensure_ascii=False)}\n\n"
        except Exception as e:
            # LLM中途挂了(超时/限流/key失效):sources已推,回答流了一半
            # 推一条error消息告诉前端"生成中断",前端可以保留已收到的部分并提示重试
            yield f"data:{json.dumps({'type':'error','message':f'生成失败:{e}'},ensure_ascii=False)}\n\n"
            return

        # 阶段5:全部完成,推done消息 
        # 前端收到done就知道"这次问答正常结束",停止loading动画,把答案固化
        yield f"data:{json.dumps({'type':'done'},ensure_ascii=False)}\n\n"

    # ===== 返回流式响应 =====
    return StreamingResponse(
        token_stream(), # 把生成器交给StreamingResponse,它来驱动yield循环
        media_type="text/event-stream", # SSE的固定MIME类型,客户端靠这个头识别"这是SSE流"
        headers={
            "Cache-Control":"no-cache", # 禁止任何中间层缓存这条流
            "X-Accel-Buffering":"no", # 给Week 6的Nginx看的:关掉代理缓冲,token立即透传
        }
    )
