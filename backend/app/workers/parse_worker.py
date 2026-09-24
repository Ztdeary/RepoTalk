"""arq 异步解析 worker:把原先阻塞在/ingest里的活搬到独立进程。
启动(另开一个终端,在backend\\下):
    uv run arq app.workers.parse_worker.WorkerSettings
"""

import asyncio # to_thread：把同步阻塞调用丢进线程池
import json # 进度消息序列化
import shutil # 清理克隆出来的临时目录

from arq.connections import RedisSettings 
# arq的Redis连接配置
# arq是异步任务队列，负责把慢任务(比如解析仓库代码)放到Redis里
# 服务崩了也不会丢，支持重试、延迟和定时任务
from langchain_openai import OpenAIEmbeddings # 向量化客户端
from langchain_postgres import PGVector # 向量库
from sqlalchemy import select # 查询repos行
from app.core.config import settings # 导入配置类
from app.core.db import AsyncSessionLocal,engine,init_db # 导入数据库会话类、引擎、建表函数
from app.models import Repo,RepoStatus # 导入仓库模型类、状态常量类
from app.rag.code_splitter import split_code_file # 导入代码分割函数
from app.rag.repo_loader import clone_repo,iter_code_file # 导入仓库克隆、文件过滤函数
from app.core.cache import invalidate_repo_cache # 解析成功后清旧检索缓存

# 进程级单例。注意:worker是独立进程，和API进程各有各的内存， 
# 所以这里必须自己构造一份，不能指望复用routes.py里的那个_embeddings。
_embeddings=OpenAIEmbeddings( # 向量化客户端
  model=settings.embedding_model, # 向量化模型
  base_url=settings.embedding_base_url, # 向量化模型的API地址
  api_key=settings.embedding_api_key, # 向量化模型的API密钥
  timeout=30, # 30秒超时
  max_retries=1, # 最多重试1次
)

def _repo_name_from_url(repo_url:str)->str:
  """从仓库URL提取仓库名,写进每个chunk的metadata.repo。"""
  return repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
  # rstrip("/")表示去除URL末尾的斜杠
  # split("/")[-1]表示按斜杠分隔,并取最后一个元素(仓库名)
  # removesuffix(".git")表示去掉.git后缀,返回仓库名

async def _emit(ctx,repo_id:int,stage:str,error:str|None=None)->None:
    """一个动作做两件事:写数据库状态+向Redis推一条进度。
    为什么两件一起做?
    数据库是"事实来源"（前端刷新页面能查到当前状态），
    Redis频道是"实时通知"(正连着的SSE立刻收到,不用轮询)。"""
    # ctx——arq框架自动塞进来的"context（上下文）"，本质是个字典
    # 里面有ctx["redis"]（框架帮你建好的Redis连接）等东西。
    # error:str|None=None表示可选str或None,并且默认值=None
    # 有默认值的参数必须排在没默认值的参数后面
    async with AsyncSessionLocal() as session:
      # 产出一个新会话对象，绑定到session变量
      # async with——异步版的with。with是"上下文管理器":
      # 进入时自动做事,离开时,无论成功还是异常都自动收尾
      repo=(
        await session.execute(select(Repo).where(Repo.id==repo_id))
      ).scalar_one_or_none()
      # 按ID查询仓库对象，然后取单条值（ORM对象）出来
      # select(Repo)——SQLAlchemy的查询构造器
      # session.execute(...)——把这条查询发给数据库执行。返回的是个"结果对象"
      # .scalar_one_or_none()——从结果对象里取数据:
      # scalar = 取单个值（ORM对象）而不是整行
      # one = 期望恰好一条
      # or_none = 查不到时返回None，而不是抛异常
      if repo is None: # 记录被删了就安静退出，别让任务炸了
        return # 退出函数，不执行后续代码
      repo.status=stage # 推进状态机
      if error is not None: # 无条件赋值:失败时写入原因,成功时(None)自动清空旧错误
        repo.error=error
      await session.commit()
      # commit——提交事务 。前面改属性只是"暂存"，这一句才真正写入数据库

    # 向Redis推一条进度消息
    payload={"repo_id":repo_id,"stage":stage}
    if error is not None:
        payload["error"]=error
    await ctx["redis"].publish(
      # .publish(频道名,消息)——Redis的发布/订阅（pub/sub）功能:
      # 往"频道"里发一条消息，所有订阅了这个频道的客户端（将来的SSE接口）立刻收到。
      f"repo:{repo_id}",
      # 每个仓库一个独立频道，互不干扰。
      json.dumps(payload,ensure_ascii=False),
      # ensure_ascii=False——不转义中文。
      # 默认True会把中文变成\u4f60\u597d这种转义序列，设False保留原文，日志可读。
    )

async def parse_repo_task(ctx,repo_id:int,repo_url:str,collection:str)->dict:
  """后台任务:克隆->切分->向量化入库->存BM25原始块->更新状态。"""
  root=None 
  # root=None——先占位。
  # root是克隆出来的临时目录路径，但克隆这一步在try里面可能失败;
  # 如果不先定义成None，后面finally里用root会报"变量未定义"。
  try: 
    # 1.克隆
    await _emit(ctx,repo_id,RepoStatus.CLONING)
    # RepoStatus.CLONING——用建的常量类取"cloning"字符串。
    # 为什么不用字面量"cloning"?
    # 防止拼错——拼错常量名会立刻报错，拼错字符串静默出错。
    root=await asyncio.to_thread(clone_repo,repo_url)
    # clone_repo是subprocess同步阻塞调用，直接await会卡死arq事件循环
    # asyncio.to_thread(函数, 参数...)——把同步函数扔到线程池里的另一个线程执行
    # 返回一个可以await的对象。
    # 效果:克隆在后台线程跑，事件循环不被卡死。

    # 2.切分
    await _emit(ctx,repo_id,RepoStatus.SPLITTING)
    repo_name=_repo_name_from_url(repo_url) # 从URL提取仓库名
    skipped=0 # skipped=0——局部变量，统计切分失败的文件数。

    def _split_all()->list[dict]:
      """同步的切分循环，整体丢进线程池执行。"""
      # def _split_all()——定义一个内部函数。
      # 为什么要在里面定义？
      # 因为它要用外层的三个变量(root,repo_name,skipped)
      # 定义在里面就能"自动捕获"它们，不用显式传参。
      nonlocal skipped
      # Python规则:函数内部给外层变量赋值(skipped+=1)，默认会创建一个新的同名局部变量，改不到外面那个。
      # nonlocal声明:"我说的skipped是外层函数那个，别新建"
      out:list[dict]=[] # 新建空列表，带类型标注:装dict的列表。
      for file in iter_code_file(root): 
        try:
          out.extend(split_code_file(file,repo_name,root))
        except Exception as e: # 单文件坏了不中断整次解析
          skipped+=1 # 切分失败的文件数+1
          print(f"[worker]切分失败，跳过{file}:{e}")
      return out

    chunks=await asyncio.to_thread(_split_all) # 把上面整个闭包丢进线程池执行

    if not chunks: # 一个块都没有=URL错了或空仓库
      raise ValueError("没有切出任何代码块，请检查仓库地址")
      # raise ValueError(...)——主动抛出异常，跳转到外层except。
      # ValueError是内置异常类型，语义是"参数值有问题"（URL错了或空仓库）。

    # 3.向量化入库
    await _emit(ctx,repo_id,RepoStatus.EMBEDDING)

    def _store_all()->None:
      """PGVector走psycopg同步驱动,同样不能直接在事件循环里跑。"""
      store=PGVector(
        embeddings=_embeddings,
        # 用文件顶部那个全局embedding客户端（它会把文本发给硅基流动转成向量）。
        collection_name=collection,
        connection=settings.database_url,
        # 数据库连接串
        #（注意:这里是psycopg同步驱动那个URL，不是db.py里asyncpg的——两个驱动各管一摊）。
        pre_delete_collection=True, # 写之前先删同名集合，保证"重新解析=整库覆盖"
      )
      store.add_texts( 
        texts=[c["content"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
      )
    
    await asyncio.to_thread(_store_all)
    # 为什么这段也要to_thread——add_texts内部要做两件阻塞事:
    # 调硅基流动API算向量（网络IO）+psycopg写库（同步驱动）。直接在事件循环里跑会卡死worker。

    # 4.把切分结果存进Redis，供API进程重建BM25。 
    # 关键原因:BM25索引在内存里，worker建好的索引API进程根本看不见。 
    # 所以不传索引、传"原料"，谁用谁重建。
    await ctx["redis"].set(
      f"bm25:{collection}",
      json.dumps(chunks,ensure_ascii=False),
      ex=7*24*60*60, 
      # 7天过期，防止Redis无限膨胀
      # 写成乘法表达式而不是直接写604800，是为了让人一眼看懂"7 天"。
    )
    # ctx["redis"].set(key,value,ex=秒数)
    # Redis最基本的SET命令:存一个字符串键值对。

    # 4.5 清掉该仓库的旧检索缓存:数据已更新,旧答案必须作废 
    # 放在emit DONE之前:保证前端看到done时,缓存里绝不会有上一代的旧答案
    try:
      deleted=await invalidate_repo_cache(ctx["redis"],repo_id,collection)
      # ctx["redis"]:arq框架建好的异步Redis连接,直接复用,不用再建客户端
      if deleted:
        print(f"[worker]已清理{deleted}条旧检索缓存,repo_id={repo_id}")
    except Exception as e:
      # 清缓存失败不代表解析失败:数据本身已全部落库,最多旧答案再活10分钟(TTL兜底)
      print(f"[worker]清旧缓存失败(不影响任务):{e}")

    # 5.收尾
    await _emit(ctx,repo_id,RepoStatus.DONE)
    return {"repo_id":repo_id,"chunks":len(chunks),"skipped":skipped}
    # return{...}——这个字典是"任务结果"
    # arq会把它序列化存进Redis（按keep_result=3600留1小时）

  except Exception as e:
    # 捕获try块里（包括_emit、克隆、切分、入库）抛出的任何异常。
    # 失败也要让前端看见，并把原因落库，方便排查
    print(f"[worker]解析失败,repo_id={repo_id}:{e}")
    await _emit(ctx,repo_id,RepoStatus.FAILED,error=str(e)[:1000])
    # str(e)[:1000]——把异常转成字符串后切片取前1000字符。
    # [:1000]是切片语法:[起点:终点]，省略起点=从头。
    # 防超长错误信息（比如整段git stderr）撑爆error列。
    raise

  finally:
    # 无论成功失败都删临时目录，否则每解析一次就漏一个仓库副本在磁盘上
    if root is not None:
      # if root is not None:防一种情况:
      # 克隆本身失败了，root还是开头占位的None，None没有目录可删。
      shutil.rmtree(root,ignore_errors=True)
      # shutil.rmtree(root,ignore_errors=True)——递归删除整个目录树
      # ignore_errors=True=删除过程中个别文件删不掉（比如被占用）也不抛异常
    
async def startup(ctx)->None:
  # startup(ctx)——名字是约定:arq启动worker进程时自动调用一次。ctx此时已经建好（里面有redis）。
  await init_db()
  # worker是独立进程、不走FastAPI的lifespan，所以必须自己建。
  # 所以表得自己建,否则第一次写库就会relation "repos" does not exist。"""

async def shutdown(ctx)->None:
  # shutdown——约定名:worker收到退出信号（Ctrl+C）时调用。
  await engine.dispose()
  # 关闭异步引擎、释放连接池里所有数据库连接。
  # 不释放的话，开发期反复重启会在Postgres里堆积僵尸连接

class WorkerSettings:
  """arq只认这个名字的类作为worker配置入口。"""
  # app.workers.parse_worker.WorkerSettings直接读这个类的类属性当配置。
  # 它就是个"配置容器"，用class而不是dict只是arq的约定。
  functions=[parse_repo_task]
  # 列表里放函数对象（没括号，不是调用）
  # 只有登记在这里的函数才允许被入队执行
  # 防止有人往Redis里塞任意函数名攻击你
  on_startup=startup # 同样传函数对象，让arq在启动时替我们调用。
  on_shutdown=shutdown # 同样传函数对象，让arq在退出时替我们调用。
  redis_settings=RedisSettings.from_dsn(settings.redis_url)
  # 把.env里那一行Redis地址字符串，翻译成arq认识的配置对象
  # 挂到它约定的redis_settings属性上，让worker知道"去哪儿取任务"。
  max_tries=1 
  # 任务失败最多试1次（=不重试）。
  # 默认arq会重试好几次，但克隆+向量化又慢又烧钱
  job_timeout=600
  # 单个任务最多跑600秒，超时强杀。防某个仓库把worker永久占住。
  keep_result=3600
  # 任务结果（return的那个dict）在Redis里保留1小时供查询，过期自动清。
