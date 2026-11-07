from fastapi import APIRouter,Depends,Header,Request # 路由/依赖注入/请求头/请求对象
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
from app.core.security import get_optional_user # 可选登录:管理员免配额,访客照常放行
from app.core.byok import ByokError,LLMCred,parse_credentials # 访客自带Key的类型与校验
from app.core.stats import consume_quota,get_visitor_id,record_question # 访客配额与统计

import json # 把每条SSE消息的业务字典序列化成JSON字符串(SSE协议只传文本)
import asyncio # 用asyncio.to_thread把同步检索丢线程池+asyncio.wait实现检索期心跳

router = APIRouter(
    # 访客模式的改动点:去掉了原来的 dependencies=[Depends(get_current_user)]
    # 因为"只读演示"要求不登录也能提问。
    # 权限判断下沉到 _chat_gate:管理员免配额;访客按"有没有自带Key"走两套额度。
    # 关键约束:所有校验都必须在流开始之前完成,
    # 一旦 StreamingResponse 开始,HTTP 状态码已经发出去,再想返回 429 就不可能了
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


def _chat_gate(
    request:Request,
    admin:str|None,
    visitor_header:str|None,
    llm_base_url:str|None,
    llm_key:str|None,
    llm_model:str|None,
    question:str,
) -> LLMCred|None:
    """问答前的统一"收费站",/chat 与 /chat/stream 共用同一套规则。

    返回:访客自带的LLM凭证;None表示"用站主的Key"。
    会抛三种异常(都在流开始之前,所以前端能收到正常HTTP状态码):
    400 - 访客自带Key格式不合法 / 问题超长
    429 - 今日提问次数用完

    角色矩阵(这是整个方案的规则表,改规则只改这里):
    管理员(token有效) -> 免配额,用站主Key
    访客+自带Key       -> 不用站主Key,但有防刷上限
    访客无Key          -> 只读演示:站主Key + 严格输出上限 + 每日额度
    """
    visitor=get_visitor_id(request,visitor_header)
    # 访客身份:优先用前端持久化的ID,没有就用IP兜底

    cred:LLMCred|None=None
    # 只有"总开关打开 + 三个头都齐"才尝试解析。
    # 少一个头就当作"访客没带Key",而不是报错——
    # 这样前端即使只配了一半,也还能退回只读演示继续用
    if settings.byok_enabled and llm_base_url and llm_key and llm_model:
        try:
            cred=parse_credentials(llm_base_url,llm_key,llm_model)
        except ByokError as exc:
            # 这里必须报错而不是静默忽略:
            # 用户明明填了Key却因为我们悄悄忽略而继续花站主的钱,是最糟的体验
            raise HTTPException(status_code=400,detail=str(exc))

    if admin is None:
        # 访客(含自带Key的):先按额度记账
        limit=(
            settings.byok_daily_question_limit
            if cred is not None
            else settings.guest_daily_question_limit
        )
        if not consume_quota("question",visitor,limit):
            hint=(
                "可配置自己的 LLM Key 提高额度"
                if cred is None
                else "自带 Key 的每日防刷上限已到"
            )
            raise HTTPException(
                status_code=429,
                detail=f"今日提问次数已用完(上限 {limit} 次),{hint}",
            )

        # 只读演示的成本红线在这里落地:问题太长会让"输入token"暴涨。
        # 自带Key的访客不受这条限制,因为花的是他自己的钱
        if cred is None and len(question)>settings.demo_max_question_chars:
            raise HTTPException(
                status_code=400,
                detail=f"演示模式下问题不能超过 {settings.demo_max_question_chars} 字,"
                       "配置自己的 LLM Key 可解除此限制",
            )

    # 统计:成功走到这里才计数(被额度拦掉的请求不算"提问")
    record_question(visitor,byok=cred is not None)
    return cred


def _demo_max_tokens(cred:LLMCred|None,admin:str|None)->int|None:
    """返回这次回答的输出 token 上限;None=用模型默认(不限制)。
    只有"无Key的普通访客"才设上限:
    管理员是站长自己,自带Key的访客花自己的钱,都没必要削他们的回答长度。
    320 个 token 约等于 200~250 字,足够解释一个函数/一段逻辑,
    单次成本能压在 0.01 元以内(远低于"两毛一次"的要求)。"""
    if cred is None and admin is None:
        return settings.demo_max_output_tokens
    return None


def _demo_docs(cred:LLMCred|None,admin:str|None,docs:list)->list:
    """只读演示时裁剪上下文块数,进一步压低输入 token。
    检索本身(top-k/rerank)不变,只是"喂给模型的资料"少几块:
    少 = 便宜,但回答依据也少,所以只对无Key访客做这个取舍。
    裁剪发生在"生成前",sources 引用列表也会同步变短,前后一致不会驴唇不对马嘴。"""
    if cred is None and admin is None:
        return docs[:settings.demo_context_k]
    return docs

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
async def chat(
    req:ChatReq,
    request:Request,
    # Request:供访客身份兜底(没有 X-Visitor-Id 时用 IP)
    session:AsyncSession=Depends(get_session),
    # Depends:FastAPI 自动注入数据库会话
    admin:str|None=Depends(get_optional_user),
    # 可选登录:管理员返回"admin",访客返回None
    x_visitor_id:str|None=Header(default=None,alias="X-Visitor-Id"),
    x_llm_base_url:str|None=Header(default=None,alias="X-LLM-Base-URL"),
    x_llm_key:str|None=Header(default=None,alias="X-LLM-Key"),
    x_llm_model:str|None=Header(default=None,alias="X-LLM-Model"),
    # 四个头:访客身份 + 三个自带Key字段。没有Key时这三个是None
): # ... 会被 FastAPI 自动注入
    """混合检索 + 生成回答。
    collection解析:优先用repo_id查repos表,没给repo_id就直接用collection字段。"""
    # 0.先收费站:校验自带Key、扣配额、拦超长问题
    # 放在查库/检索之前:被拒绝的请求不应该产生任何数据库/API开销
    cred=_chat_gate(
        request,admin,x_visitor_id,x_llm_base_url,x_llm_key,x_llm_model,req.question,
    )

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
            detail="必须提供repo_id或collection其中之一"
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
    
    # 5.成本控制:只读演示的访客只喂前 demo_context_k 块
    docs=_demo_docs(cred,admin,docs)

    # 6.生成:无论docs来自缓存还是现查,生成这一步都要现场做 
    # generate_answer是同步阻塞(chain.invoke等LLM写完整篇),同样丢线程池
    loop=asyncio.get_running_loop()
    # loop:接收拿到的事件循环对象,后面要用它调度线程池 
    # get_running_loop():获取"当前正在运行的事件循环" 
    # async def路由跑起来时FastAPI已把它放进事件循环,这个函数把那个循环对象取出来
    return await loop.run_in_executor(
        None, # None=用事件循环默认线程池,省得自己管理线程池生命周期
        generate_answer, # 要扔进线程池执行的同步函数
        req.question,docs, # 传给generate_answer的前两个位置参数
        cred, # 第三个位置参数:访客自带凭证;None=用站主的Key
        _demo_max_tokens(cred,admin), # 第四个:演示模式下的输出上限;None=不限制
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
        detail="必须提供repo_id或collection其中之一"
    )

@router.post("/chat/stream",description="SSE流式问答:先推引用来源,再逐token推回答")
async def chat_stream(
    req:ChatReq,
    request:Request,
    session:AsyncSession=Depends(get_session),
    admin:str|None=Depends(get_optional_user),
    x_visitor_id:str|None=Header(default=None,alias="X-Visitor-Id"),
    x_llm_base_url:str|None=Header(default=None,alias="X-LLM-Base-URL"),
    x_llm_key:str|None=Header(default=None,alias="X-LLM-Key"),
    x_llm_model:str|None=Header(default=None,alias="X-LLM-Model"),
):
    """POST/chat/stream——流式版问答。
    与/chat的唯一区别是"输出方式":
    /chat等LLM写完整篇回答再一次返回JSON;
    本接口检索完立刻推sources,然后LLM每吐一个词就推一条SSE消息。
    检索逻辑、prompt、LLM参数全部复用,只变传输,不变智能。

    访客模式新增:与/chat共用同一个 _chat_gate。
    特别注意顺序——gate 必须在 return StreamingResponse 之前跑完,
    否则额度用尽时状态码已经发出去,只能让前端看到一条断掉的流。"""
    # ===== 流开始前:配额/Key校验(失败抛HTTP异常,前端能正常显示原因) =====
    cred=_chat_gate(
        request,admin,x_visitor_id,x_llm_base_url,x_llm_key,x_llm_model,req.question,
    )

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

        # 阶段2.5:成本裁剪——只读演示只喂前 demo_context_k 块。
        # 必须放在 sources 之前:否则会推出"引用了6块,但只按4块作答"的错位信息
        docs=_demo_docs(cred,admin,docs)

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
            # 第3、4个参数:访客自带凭证 + 演示模式的输出上限。
            # pipeline 内部据此决定用谁的Key、要不要给回答"限长"
            async for token in stream_answer_hybrid(
                req.question,docs,cred,_demo_max_tokens(cred,admin),
            ):
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
