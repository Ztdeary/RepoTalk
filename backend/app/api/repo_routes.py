# Week 4新增:repos管理路由
# 核心动作:
# POST/repos->建Repo记录+enqueue_job->立即返回repo_id
# GET/repos/{id}-> 查单个 repo 的实时状态
# GET/repos-> 列出所有repos(给前端选仓库用)
# 所有路由都用async def,因为repos表是SQLAlchemy2.0 async session

import json
# json:把"来晚了"兜底场景的状态字典序列化成JSON字符串
# (实时消息是worker发过来的原样字符串,不用json;但兜底那条要我们自己造)

import asyncio
# asyncio:删除仓库时要调 PGVector 的同步客户端(psycopg),
# 必须丢进线程池,否则会卡死整个事件循环(同 worker 里 to_thread 的理由)

from langchain_core.embeddings import Embeddings
# Embeddings:向量化接口的抽象基类。
# 删除 collection 时 PGVector 的构造函数要求传一个 embeddings 参数,
# 但 delete_collection 根本不会调用向量化,所以传一个"拒绝工作"的占位实现即可,
# 好处是删除动作完全不消耗 embedding 额度

from langchain_postgres import PGVector
# PGVector:和 worker 入库时用的是同一个类;
# 用它删 collection 会自动级联删掉 langchain_pg_embedding 里的向量行
# (数据库外键是 ON DELETE CASCADE,已在数据库里确认过)

from urllib.parse import urlparse
# urlparse:把仓库地址拆成 协议/域名/路径 三段,
# 用来校验"只允许 https + 知名代码托管站",防止有人让我们去 clone 内网地址

from fastapi import APIRouter,Depends,Header,HTTPException,Request
# APIRouter:FastAPI里的"迷你路由器"，可以给一组路由统一加prefix
# Depends:FastAPI依赖注入关键字，用来声明"这个参数由框架自动提供，不是调用方传的"
# Header:声明参数从请求头取(访客ID / 自带Key)
# HTTPException:校验失败时抛出,自动变成对应状态码的JSON
# Request:拿客户端IP,给"没有访客ID"的请求兜底身份
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
from app.core.security import get_current_user, get_optional_user
# get_current_user:必须登录(用于删除这类仅管理员可做的写操作)
# get_optional_user:可选登录(管理员免配额,访客放行)
from app.core.byok import ByokError,parse_credentials # 访客自带Key的校验
# settings 已在文件上方导入(原本用于读 REDIS_URL),
# 这里复用同一个实例来读 byok_enabled / guest_daily_repo_limit,保证与 .env 完全一致
from app.core.stats import consume_quota,get_visitor_id,record_repo # 配额与统计
from app.core.cache import invalidate_repo_cache
# invalidate_repo_cache:清掉该仓库在 Redis 里的检索结果缓存(retrieve:repo:* / retrieve:coll:*)
# 删除仓库后如果不清,别人再问同一个问题会命中"已删仓库"的旧答案
from app.rag.hybrid_retriever import drop_bm25
# drop_bm25:同时清掉进程内存和 Redis 里的 BM25 索引,
# 只删 Redis 的话本进程内存里还留着旧索引(见 hybrid_retriever.drop_bm25 注释)


# 允许被 clone 的代码托管站域名。
# 为什么必须限制:repo_url 是访客可控输入,worker 会拿它执行 git clone;
# 不限制的话,访客填 http://169.254.169.254/... 之类,就能让我们去访问云厂商元数据接口
ALLOWED_REPO_HOSTS={"github.com","gitee.com","gitlab.com"}

def _validate_repo_url(raw:str)->str:
  """校验并清洗仓库地址:必须 https + 白名单域名 + 不带账号密码。
  返回清洗后的地址(去空格/反引号/尾斜杠);不合法直接抛400。"""
  url=raw.strip().strip("`").strip() # 三级清理:去空格->去反引号->再去空格
  parsed=urlparse(url)
  if parsed.scheme!="https":
    raise HTTPException(status_code=400,detail="仓库地址必须以 https:// 开头")
  if parsed.username or parsed.password:
    # https://user:token@github.com/... 这种写法会把凭据带进 git 命令,
    # 既可能泄漏,也会绕过"只看域名"的白名单判断
    raise HTTPException(status_code=400,detail="仓库地址不能包含用户名或密码")
  host=(parsed.hostname or "").lower()
  if host not in ALLOWED_REPO_HOSTS:
    raise HTTPException(
      status_code=400,
      detail=f"目前只支持 {', '.join(sorted(ALLOWED_REPO_HOSTS))} 的公开仓库",
    )
  if not parsed.path or parsed.path=="/":
    raise HTTPException(status_code=400,detail="仓库地址缺少仓库路径")
  return url

class _DeleteOnlyEmbeddings(Embeddings):
  """删除仓库时用的"占位向量器":一旦被真正调用就抛错。

  为什么要这个类:PGVector 的构造函数必须收一个 embeddings,
  但 delete_collection() 内部只做数据库删除,压根不会向量化。
  给它传真实的 OpenAIEmbeddings 既要求 .env 里有 Key,
  也会让人误以为"删除会调用 embedding 接口"。
  用这个类表达意图:删除路径绝不产生任何模型调用与费用。"""

  def embed_documents(self,texts:list[str])->list[list[float]]:
    raise RuntimeError("删除仓库时不应触发向量化")

  def embed_query(self,text:str)->list[float]:
    raise RuntimeError("删除仓库时不应触发向量化")

def _drop_vector_collection(collection:str)->None:
  """删除 pgvector 里的整个 collection(collection 表 + 级联的向量行)。

  必须用 PGVector 而不是手写 DELETE:
  1) 表名/字段名是 langchain-postgres 的内部约定,手写 SQL 哪天库升级就失效
  2) collection 与 embedding 之间是外键级联删除,交给库处理最稳
  注意:本函数是同步阻塞的(psycopg),调用方必须用 asyncio.to_thread 包起来。"""
  store=PGVector(
    embeddings=_DeleteOnlyEmbeddings(), # 占位,不会被调用
    collection_name=collection,
    connection=settings.database_url, # psycopg 同步连接串(和 worker 入库时一致)
  )
  store.delete_collection()
  # 找不到 collection 时库内部只打 warning 直接返回,不抛异常,
  # 所以"仓库从没解析成功、根本没有向量数据"的情况也能安全删除

router=APIRouter(
  # APIRouter:调用构造函数，创建一个迷你路由器
  prefix="/repos",
  # prefix:让所有路由自动加/repos 前缀
  tags=["repos"],
  # tags参数:Swagger UI里的分组标签，把同组路由折叠到一起
  # 注意:这里故意不再挂 dependencies=[Depends(get_current_user)]
  # 访客模式的改动点:GET 列表/详情/SSE 进度对所有人开放(只读演示),
  # 写操作(POST 提交仓库)在函数内部单独做权限判断:
  # 管理员token OR 访客自带Key,两种身份走两套规则
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
  request:Request,
  # 请求对象:拿客户端IP,给没有 X-Visitor-Id 的调用方兜底一个访客身份
  session:AsyncSession=Depends(get_session),
  # Depends:依赖注入
  # 不是调用方传的，FastAPI自动调get_session()
  # 拿到session对象塞进来。每个请求一个新会话，用完自动关闭
  pool:ArqRedis=Depends(get_pool),
  # 同上。
  # FastAPI自动调get_pool()拿到连接池单例塞进来
  admin:str|None=Depends(get_optional_user),
  # 可选登录:管理员(token有效)返回"admin";访客返回None。
  # 这是"管理员免限制、访客走自带Key"的分岔点
  x_visitor_id:str|None=Header(default=None,alias="X-Visitor-Id"),
  # alias:请求头真实名字是 X-Visitor-Id,Python 变量名不能带短横线,所以用 alias 映射
  x_llm_base_url:str|None=Header(default=None,alias="X-LLM-Base-URL"),
  x_llm_key:str|None=Header(default=None,alias="X-LLM-Key"),
  x_llm_model:str|None=Header(default=None,alias="X-LLM-Model"),
  # 三个自带 Key 的头:只有访客提交仓库时需要(解析本身用站主的 embedding 额度)
):
  """POST/repos——核心入口:
  1. 权限分流:管理员免配额;访客必须自带 LLM Key 且受每日提交数限制
  2. 校验仓库地址(https + 知名托管站白名单,防 SSRF)
  3. 从URL提取仓库名作为collection(调用方没给的话)
  4. 建Repo记录(status=pending)+ enqueue_job丢给worker
  5. 立即返回{repo_id,status:pending}
  为什么要这么快返回?验收标准要求"提交<1秒"。
  真正的解析在worker里慢慢跑,SSE频道repo:{repo_id}推进度。"""
  # ===== 权限分流 =====
  visitor=get_visitor_id(request,x_visitor_id)
  if admin is None:
    # 访客路径:必须自带 Key。
    # 为什么访客提交仓库也要 Key:提交动作本身不花钱,
    # 但它会触发 worker 用"站主的 embedding 额度"做向量化。
    # 要求带 Key 是一种"实名门槛",能挡掉绝大部分随手刷接口的脚本
    if not settings.byok_enabled:
      raise HTTPException(
        status_code=403,
        detail="本站暂时只开放浏览与问答,提交新仓库功能未启用",
      )
    if not (x_llm_base_url and x_llm_key and x_llm_model):
      raise HTTPException(
        status_code=403,
        detail="提交新仓库需要先在右上角「自带 Key」里配置你自己的 LLM Key",
      )
    try:
      # 这里只做校验,不保存也不使用:提交阶段不需要真的调 LLM,
      # 校验是为了让"带了 Key"这件事不是随便填三个字符串就能糊弄过去
      parse_credentials(x_llm_base_url,x_llm_key,x_llm_model)
    except ByokError as exc:
      raise HTTPException(status_code=400,detail=str(exc))

    if not consume_quota("repo",visitor,settings.guest_daily_repo_limit):
      raise HTTPException(
        status_code=429,
        detail=f"今日提交仓库次数已用完(每人每天 {settings.guest_daily_repo_limit} 个)",
      )

  # ===== 地址校验 =====
  repo_url=_validate_repo_url(req.repo_url)

  # 自动从URL提取仓库名当collection
  # https://github.com/psf/requests->requests
  collection=(
    (req.collection or "").strip()
    or repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
  )
  if len(collection)>60:
    # PGVector 的 collection 名会进数据库标识,过长既占空间也难看;
    # 60 足够覆盖正常仓库名
    raise HTTPException(status_code=400,detail="collection 名过长(最多60字符)")

  # 建Repo记录,status默认pending(SQLAlchemy模型里定义了default)
  repo=Repo(
    repo_url=repo_url, # 上面已清洗并校验过,直接用
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

  # 记录统计:进这里说明仓库真的建成功了(前面的校验/入队都没出错),
  # 放在最后而不是开头,统计数字才不会把"提交失败"也算进去
  record_repo(visitor)

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

@router.delete("/{repo_id}",description="删除仓库及其检索数据(仅管理员)")
async def delete_repo(
  repo_id:int,
  # 路径参数:要删哪个仓库
  session:AsyncSession=Depends(get_session),
  pool:ArqRedis=Depends(get_pool),
  _admin:str=Depends(get_current_user),
  # get_current_user(不是 get_optional_user):
  # 删除是不可逆操作,且仓库是所有人共享的,必须只有管理员能做。
  # 访客没有 token 时这里直接 401,连"删除按钮"的前端也不会给他们看
):
  """DELETE/repos/{repo_id}——删除仓库,并清掉它的全部检索数据。

  删除一个仓库要清三层数据,少清一层就会留下脏数据:
  1) 数据库行 repos:列表、问答鉴权都查它,这是"是否还存在"的唯一真相
  2) pgvector collection:向量检索数据,不清的话新仓库同名时会串数据
  3) Redis:BM25 原料(bm25:{collection})和检索结果缓存(retrieve:*)

  操作顺序:先删数据库行,再尽力清外部数据。
  为什么反过来不行:外部数据删除可能因为网络/数据库慢而失败,
  如果先删向量再删行,一旦行没删掉,用户会看到一个"存在但搜不到任何东西"的仓库;
  而先删行、清理失败,最多留下一些看不见的孤儿向量(不影响使用,可后续清理)。

  任何状态都能删(2026-11 起):pending/cloning/splitting/embedding 也允许,
  否则卡在解析中的仓库没法清理。删掉 repos 行后,worker 每次推进阶段前
  都会查一次仓库是否还在,发现被删就立刻停(见 parse_worker._emit),
  不会再继续往 collection 写数据。"""
  repo=(
    await session.execute(select(Repo).where(Repo.id==repo_id))
  ).scalar_one_or_none()
  if repo is None:
    raise HTTPException(status_code=404,detail=f"repo_id={repo_id}不存在")

  collection=repo.collection # 先存下来:commit 之后 ORM 对象会过期,再读字段可能报错

  # 同一个 collection 可能被多条记录共用(collection 默认取仓库名,同名仓库会撞)。
  # 有别人还在用,就只删这一行记录,绝不能删共享的向量/索引
  shared=(
    await session.execute(
      select(Repo.id)
      .where(Repo.collection==collection,Repo.id!=repo_id)
      .limit(1)
      # limit(1):只关心"有没有别人",不需要把同名的全查出来
    )
  ).scalar_one_or_none()

  await session.delete(repo) # 标记删除
  await session.commit()     # 真正落库:这一步成功后,仓库在列表里就消失了

  vector_removed=False
  if shared is None:
    # 1) 删向量集合。PGVector 是同步阻塞调用,必须丢线程池
    try:
      await asyncio.to_thread(_drop_vector_collection,collection)
      vector_removed=True
    except Exception as exc:
      # 数据库行已删,这里失败不回滚(见 docstring 的顺序说明),只记录日志
      print(f"[repo]删除向量集合失败,collection={collection}:{exc}")

    # 2) 清 BM25(内存 + Redis)
    drop_bm25(collection)

    # 3) 清检索结果缓存(retrieve:repo:{id}:* 与 retrieve:coll:{name}:*)
    try:
      deleted=await invalidate_repo_cache(pool,repo_id,collection)
      print(f"[repo]删除仓库{repo_id}的缓存 {deleted} 条")
    except Exception as exc:
      print(f"[repo]清检索缓存失败,repo_id={repo_id}:{exc}")

  return {
    "ok":True,
    "repo_id":repo_id,
    "collection":collection,
    "vector_removed":vector_removed, # false 代表向量数据没清掉或与别人共用
    "shared_collection":shared is not None,
  }

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
