from collections.abc import AsyncIterator # 依赖注入要返回的异步迭代器类型
from sqlalchemy.orm import DeclarativeBase # 声明式基类,用于定义数据库模型
from app.core.config import settings # 导入配置类,用于获取数据库连接信息
from sqlalchemy.ext.asyncio import(
  AsyncEngine, # 异步引擎类型(仅用于类型标注)
  AsyncSession, 
  # 异步会话
  # "会话"就是你跟数据库之间一次"对话上下文"：增删改都先丢进会话，最后commit一把落库
  async_sessionmaker, # 异步会话工厂->每次需要会话就AsyncSessionLocal()调一下产一个新的
  create_async_engine, 
  # 创建异步引擎的函数。引擎=数据库连接池+驱动管理的总入口
)
# langchain-postgres用的是psycopg(同步驱动),我们这里用asyncpg(异步驱动)。 
# 直接把"+psycopg"替换成"+asyncpg",复用.env 里同一份账号密码, 
# 就不用再往.env 里加重复的URL,避免以后改一处忘了另一处(配置漂移)。
ASYNC_DATABASE_URL=settings.database_url.replace("+psycopg","+asyncpg")

# create_async_engine()->创建异步引擎:整个进程只建一个,内部自带连接池。 
# pool_pre_ping:每次从池里借连接前先探一次活,防止拿到已被数据库断掉的死连接。
engine: AsyncEngine = create_async_engine(ASYNC_DATABASE_URL, pool_pre_ping=True)

# 会话工厂:用它产出的会话在with块结束时自动把连接还给池子。 
# expire_on_commit=False:commit之后对象属性仍可读, 
# 否则commit后访问repo.id会再触发一次查询(经典绊脚坑)。
# engine->以后造出来的会话都从这个引擎的连接池借连接"
AsyncSessionLocal=async_sessionmaker(engine,expire_on_commit=False)

# 所有模型继承Base并将元数据注册到Base.metadata,方便建表
class Base(DeclarativeBase):
    """所有ORM模型的基类。所有子类会自动注册到Base.metadata,
    后面一句create_all就能把它们的表全建出来。"""

# 构造建表函数
async def init_db()->None: # None->异步函数无返回值
  """启动时建表,等价于CREATE TABLE IF NOT EXISTS(已存在就跳过)。"""
  # 在函数内部(而不是文件顶部)import模型,是为了避开循环导入: 
  # models->core.db->models会成环,放到运行时导入就绕开了。 
  # 这一步只为了"副作用"——让Repo类注册进Base.metadata,否则create_all看不见它。
  import app.models # noqa: F401
  # noqa: F401——告诉linter"我知道这个import没被用，别警告我"
  #（因为我们就是要副作用，不是要用它）

  async with engine.begin() as conn:
    # 从引擎借一个连接,并自动开启事务:块正常结束自动commit,抛异常自动rollback,连接自动归还
    await conn.run_sync(Base.metadata.create_all)
    # run_sync把同步的create_all丢进线程池执行,不阻塞事件循环。

async def get_session()->AsyncIterator[AsyncSession]:
  # 这是个异步生成器(因为有yield),返回类型是"能产出 AsyncSession的迭代器"
    """FastAPI依赖:每个请求开一个会话,请求结束自动关闭并归还连接。
    用async with保证即使路由里抛异常,连接也不会泄漏。"""
    async with AsyncSessionLocal() as session:
      # 调工厂产一个新会话，用async with包起来,确保在with块结束时自动关闭连接并还给池子
        yield session # 把会话交出去,让路由里用