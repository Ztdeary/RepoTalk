# Week 4新增:repos管理路由
# 核心动作:
# POST/repos->建Repo记录+enqueue_job->立即返回repo_id
# GET/repos/{id}-> 查单个 repo 的实时状态
# GET/repos-> 列出所有repos(给前端选仓库用)
# 所有路由都用async def,因为repos表是SQLAlchemy2.0 async session

import json
# json:把"来晚了"兜底场景的状态字典序列化成JSON字符串
# (实时消息是worker发过来的原样字符串,不用json;但兜底那条要我们自己造)

from fastapi import APIRouter,Depends
# APIRouter:FastAPI里的"迷你路由器"，可以给一组路由统一加prefix
# Depends:FastAPI依赖注入关键字，用来声明"这个参数由框架自动提供，不是调用方传的"
from pydantic import BaseModel,Field
# BaseModel:pydantic模型基类，所有请求/响应模型都继承它
# Field:给模型字段加校验规则（如min_length）和文档描述的函数
from sqlalchemy import select # select:SQLAlchemy 2.0的查询构造函数
from sqlalchemy.ext.asyncio import AsyncSession # 异步数据库会话类型
from arq import create_pool
# arq:纯async的Redis任务队列库
# create_pool:创建Redis连接池的async函数，返回一个ConnectionPool对象
from arq.connections import RedisSettings,ArqRedis # Redis 连接配置
from app.core.config import settings # 读.env 的REDIS_URL
from app.core.db import get_session, AsyncSessionLocal  # 前者是请求级依赖.后者是工厂:可自己with一个短会话
from app.models import Repo,RepoStatus # Repo ORM对象+状态常量
from fastapi.responses import StreamingResponse
# StreamingResponse:FastAPI的流式响应类。
# 普通响应是"攒齐一整份再一次发完";它是"你yield一条我就立刻推一条"
# 这正是SSE能实时刷进度的底层机制
from app.core.security import get_current_user # 从依赖注入里获取当前用户

router=APIRouter(
  # APIRouter:调用构造函数，创建一个迷你路由器
  prefix="/repos",
  # prefix:让所有路由自动加/repos 前缀
  tags=["repos"],
  # tags参数:Swagger UI里的分组标签，把同组路由折叠到一起
  dependencies=[Depends(get_current_user)],
  # router级门禁:本router下所有路由(POST提交解析/GET查状态/GET列表/GET SSE进度) 
  # 都必须带有效token,没带或无效直接401,路由函数不会执行 
  # 好处:以后往这个router加任何新路由,自动继承门禁,想忘都忘不掉
)

# arq Redis连接池:进程级单例,避免每个请求都重建连接
# 为什么不放到FastAPI lifespan 里初始化?
# create_pool本身就是async的,在lifespan里初始化是更好的做法
# 但为了让这个文件能独立运行、不依赖main.py的启动流程
# 我们用get_pool()+懒加载+finalizer的经典单例模式。

_pool:ArqRedis|None=None
# _pool:Redis连接池单例，划线开头是约定私有，外部别直接访问
# ArqRedis:这里实际应该写arq.ConnectionPool，但你用了基类类型作标注，功能等价

async def get_pool()->ArqRedis:
  """获取arq Redis连接池单例,首次调用时创建,后续复用。"""
  # 整体逻辑:这是经典的懒加载单例模式(lazy singleton)->第一次调才创建，之后一直复用。
  global _pool
  # 为什么需要global？
  # 如果不写，Python 会把函数里_pool=...这行当成"创建了一个局部变量"改的是局部的
  # 模块级那个永远是None。global强制它去改外面的。
  if _pool is None: # 如果池不存在
    _pool=await create_pool(
      RedisSettings.from_dsn(settings.redis_url)
      # 先读settings.redis_url->传给RedisSettings.from_dsn拿到配置对象
      # 传给create_pool创建连接池->await等它就绪->赋值给_pool。
    )
  return _pool # 返回连接池

TERMINAL_STAGES={RepoStatus.DONE,RepoStatus.FAILED}
# 终态集合:done(成功)和failed(失败)。
# 收到这两个状态之一=解析彻底结束,推完这最后一条消息就该主动断流。
# 用集合(set)而不是列表:"x in 集合"语义就是"是否属于",且查找更快。

class RepoCreate(BaseModel):
  """POST/repos请求体。collection不传就自动从URL提取仓库名。"""
  repo_url:str=Field(min_length=10,description="Git仓库HTTPS地址")
  collection:str|None=Field(default=None,description="PGVector collection名,默认=URL最后一段")

class RepoResp(BaseModel):
    """所有repos路由的统一响应结构。
    让前端拿到的数据形状一致,不用猜这次字段多了还是少了。"""
    id:int
    repo_url:str
    collection:str
    status:str
    error:str|None=None
    created_at:str
    updated_at:str

def _repo_to_resp(repo:Repo)->RepoResp:
    """ORM对象->API响应对象(转成dict再构造,DateTime自动转ISO字符串)。
    为什么要这个函数?
    ORM对象Repo里有datetime类型的字段,
    直接返回JSON会炸(JSON没有datetime类型)。
    .isoformat()把datetime转成
    "2026-10-23T12:00:00+08:00"这样的ISO字符串,JSON能吃。"""
    return RepoResp(
      id=repo.id,
      repo_url=repo.repo_url,
      collection=repo.collection,
      status=repo.status,
      error=repo.error,
      created_at=repo.created_at.isoformat(),
      updated_at=repo.updated_at.isoformat(),
    )

@router.post("",response_model=RepoResp,description="创建仓库解析任务")
async def create_repo(
  req:RepoCreate,
  # 请求体
  # FastAPI看到是Pydantic类型就自动从HTTP请求体解析JSON、校验
  session:AsyncSession=Depends(get_session),
  # Depends:依赖注入
  # 不是调用方传的，FastAPI自动调get_session()
  # 拿到session对象塞进来。每个请求一个新会话，用完自动关闭
  pool:ArqRedis=Depends(get_pool),
  # 同上。
  # FastAPI自动调get_pool()拿到连接池单例塞进来
):
  """POST/repos——核心入口:
  1. 从URL提取仓库名作为collection(调用方没给的话)
  2. 建Repo记录(status=pending)
  3. enqueue_job丢给worker
  4. 立即返回{repo_id,status:pending}
  为什么要这么快返回?验收标准要求"提交<1秒"。
  真正的解析在worker里慢慢跑,SSE频道repo:{repo_id}推进度。"""
  # 自动从URL提取仓库名当collection
  # https://github.com/psf/requests->requests
  collection=req.collection or(
    req.repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
  )

  # 建Repo记录,status默认pending(SQLAlchemy模型里定义了default)
  repo=Repo(
    repo_url=req.repo_url.strip().strip("`").strip(),
    # 三级清理:先去首尾空格->再去首尾反引号->再去一次空格。
    collection=collection,
  )
  session.add(repo) # 把Repo对象挂到会话里,此时还没发SQL
  await session.commit() # 真正发SQL INSERT,落库
  await session.refresh(repo) # 从数据库回读最新状态->数据库自动生成的id/created_at

  # 入队:这一行就是把任务丢进Redis,立刻返回job_id,不阻塞
  await pool.enqueue_job(
    # await pool.enqueue_job(...) 执行完
    # 任务就进Redis了,worker看到后会被触发。
    # 这行之后立刻往下走,不等worker干活
    # .enqueue_job():arq的入队方法,这个方法本身很快
    "parse_repo_task",
    # 字符串任务名,必须精确匹配（拼写、大小写），worker才知道要调哪个函数
    repo.id,repo.repo_url,repo.collection,
    # 就是worker的parse_repo_task(ctx, repo_id, repo_url, collection)后三个位置参数
  )
  print(f"[repos]已入队,repo_id={repo.id},collection={repo.collection}")
  return _repo_to_resp(repo)
  # FastAPI拿到RepoResp对象，自动按response_model=RepoResp校验字段
  # 转成JSON发出去。repo.status此时就是数据库默认的"pending"。

@router.get("/{repo_id}",response_model=RepoResp, description= "查询单个仓库状态")
# URL是"/{repo_id}"——路径参数，大括号里的repo_id自动从URL/repos/5里提取
async def get_repo(
  repo_id:int,
  # FastAPI自动把"5"字符串转成整数5，没转成就422
  session:AsyncSession=Depends(get_session),
):
  """GET/repos/{id}——查实时状态。
  给前端做轮询兜底用(虽然SSE是主力,但用户刷新页面后SSE断了,就要查这一条)。"""
  repo=(
    await session.execute(select(Repo).where(Repo.id==repo_id))
  ).scalar_one_or_none()
  if repo is None:
    from fastapi import HTTPException
    raise HTTPException(
      status_code=404,
      # 用户查了一个不存在的id，明确返回404，让前端知道"这个仓库没了"。
      detail=f"repo_id={repo_id}不存在"
    )
  return _repo_to_resp(repo)

@router.get("",response_model=list[RepoResp],description="查询所有仓库状态")
# 这次返回的是列表,不是单个。FastAPI会对列表里的每个元素都校验RepoResp结构
async def list_repos(
  session:AsyncSession=Depends(get_session),
):
  """GET/repos——列出所有仓库,按created_at倒序(新的在前)。
  前端工作台展示"我解析过的仓库列表"就靠这个接口。"""
  rows=(
    (await session.execute(select(Repo).order_by(Repo.created_at.desc())))
    # Repo.created_at.desc():按创建时间倒序，新的在前
    .scalars() # 把Row对象剥掉,直接给Repo对象
    .all() # 全部取出,list[Repo]
  )
  return [_repo_to_resp(r) for r in rows]

# 路由外壳(先查库兜底)
@router.get("/{repo_id}/events",description="SSE订阅仓库解析进度")
# 这段的目的:
# 进生成器之前先做预检。404必须在流开始前抛出
# 一旦流式响应开始了,HTTP状态码就已经发出去了,想再返回404是不可能的
# URL完整形态:/repos/5/events。注意它必须写在@router.get("/{repo_id}")之后吗?
# 不需要——FastAPI按"更具体的路径优先"匹配,/5/events段数更多,不会和/{repo_id}混淆。
async def repo_events(
  repo_id:int, # 路径参数,从URL自动提取并转成int
  session:AsyncSession=Depends(get_session), # 注入数据库会话(查状态兜底用)
  pool:ArqRedis=Depends(get_pool), # 注入Redis连接池单例(订频道用)
):
  """GET/repos/{repo_id}/events——SSE实时推送解析进度。
  浏览器地址栏直接打开这个URL就能看到消息一条条刷出来,验收零工具。"""
  repo=(
    await session.execute(select(Repo).where(Repo.id==repo_id))
  ).scalar_one_or_none() # 查不到返回None而不是抛异常
  if repo is None:
    from fastapi import HTTPException
    raise HTTPException(
      status_code=404,
      detail=f"repo_id={repo_id}不存在"
    )# 订阅一个不存在的仓库毫无意义,直接404

  async def event_stream():
    """异步生成器:函数体里有yield,它就不是普通函数而是生成器工厂。
    StreamingResponse会"驱动"它:每执行到一次yield,就把那一条数据立刻推给浏览器,
    然后函数暂停在yield处,等下一条消息到来再继续。"""
    if repo.status in TERMINAL_STAGES:
      # 情况1:来晚了(快路径)——解析早已结束,频道里永远不会有新消息了
      # 从数据库把终态补发一条,然后立刻结束,别让浏览器傻等
      payload={"repo_id":repo_id,"stage":repo.status,"error":repo.error}
      # 注意:这里是"赋值给变量",不是"当参数传",所以不能写json.dumps(payload={...})
      yield f"data:{json.dumps(payload,ensure_ascii=False)}\n\n"
      # SSE协议的硬性格式:每条消息必须以"data:"开头、以两个换行"\n\n"结尾,
      # 少一个换行浏览器都认不出来这是一条完整消息。
      return # 生成器return=流结束,浏览器收到EOF

    # 情况2:正在解析中——先订阅频道,再复查状态(顺序不能反)
    channel=f"repo:{repo_id}" # 频道名,必须和worker里publish的一字不差
    pubsub=pool.pubsub() # 从连接池开一条"订阅专用连接"
    # 为什么不直接用pool.subscribe?Redis协议规定:连接一旦进入订阅模式
    # 就不能再干别的(不能set/get)。所以订阅要单独开一条连接,pool留着自己用
    await pubsub.subscribe(channel) # 先上频道,保证这之后的消息一条都不会漏

    try:
      # 订阅成功后复查:堵住"预检→订阅"之间的窗口
      # worker可能恰好在这个窗口里发出了终态消息,那时我们还不在频道上
      async with AsyncSessionLocal() as fresh_session:
        latest=(
          await fresh_session.execute(select(Repo).where(Repo.id==repo_id))
        ).scalar_one_or_none()
      if latest is not None and latest.status in TERMINAL_STAGES:
        payload={"repo_id":repo_id,"stage":latest.status,"error":latest.error}
        yield f"data:{json.dumps(payload,ensure_ascii=False)}\n\n"
        return # 走finally:退订+关连接,不会泄漏

      # 消息循环+心跳+收尾
      idle=0.0 # 距上一条消息过去了多少秒(心跳计时器)
      while True:
        msg=await pubsub.get_message(
          ignore_subscribe_messages=True,
          timeout=1.0
          # get_message:"最多等1秒,有消息就返回,没有就返回None"。
          # ignore_subscribe_messages=True:过滤掉"订阅成功"这类系统确认帧,只留真消息。
          # await:等待期间事件循环是自由的,可以同时伺候别人,不阻塞服务。
        )
        if msg is None: # 这1秒频道里没动静
          idle+=1.0 # 累加1秒心跳计时
          if idle>=15: # 连续15秒没消息
            yield ": ping\n\n"
            # SSE协议:冒号开头的行是注释,浏览器自动忽略
            # 但字节在流上真实流过,防止代理或浏览器因为长时间没数据而自动断开连接。
            idle=0.0 # 重置心跳计时器
          continue # 回到循环顶部继续等
        idle=0.0 # 来真消息了,心跳计时清零
        data=msg["data"] # 提取消息内容
        if isinstance(data,bytes):
          # redis默认返回bytes(二进制),转回str
          # isinstance():判断变量是否是某种类型,是否是某种类型的实例
          data=data.decode("utf-8") # 转换为字符串
        yield f"data:{data}\n\n" # data就是worker publish的原样JSON字符串,透传即可
        if json.loads(data)["stage"] in TERMINAL_STAGES:
          break # 终态消息已送达,主动结束循环,流关闭
    finally:
      # 无论循环是break结束、客户端断开、还是中途异常,都必须释放订阅连接。
      # 不释放的话,客户端每断一次就泄漏一条Redis连接。
      await pubsub.unsubscribe(channel) # 退订频道
      await pubsub.aclose() # 关闭订阅连接(老版本redis没有aclose,就改成close())

  # 返回流式响应
  return StreamingResponse(
    event_stream(), # 把生成器交给它,它来驱动yield循环
    media_type="text/event-stream", # SSE的固定MIME类型,浏览器靠这个头识别"这是SSE流"
    headers={
      "Cache-Control":"no-cache", # 禁止任何中间层缓存这条流(缓存了进度就不是实时了)
      "X-Accel-Buffering":"no",
      # 给Week 6的Nginx看的:关掉代理缓冲,消息立即透传
      # 现在加它无害,部署时少踩一个"SSE消息攒一堆才到"的坑
    },
  )
