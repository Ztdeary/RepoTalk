"""检索结果缓存:把hybrid_search的产物(docs列表)存进Redis。
为什么缓存检索结果而不是最终回答?
- 检索是最贵的一段:embedding API+BM25+rerank HTTP,一次1~3秒
- 回答交给LLM现场生成,保证prompt/参数改了立刻生效,不被旧答案冻住
- /chat和/chat/stream两个接口都要docs,缓存docs一处命中两处受益"""

import hashlib # 哈希算法库:把任意长的问题文本压成定长指纹,避免key里出现中文/空格/超长文本
import json # Document对象不能直接JSON化,要拆成普通dict才能存
import redis as _redis # 同步客户端:和hybrid_retriever里的_bm25_redis_client同款
# 为什么用同步而不是redis.asyncio? 
# GET/SET是亚毫秒级本地调用,和_bm25_cache_hit在async路由里直接调同步redis是同一取舍 
# 全链路async当然更纯正,但为两次亚毫秒调用引入await样板代码不值

from langchain_core.documents import Document # 缓存命中后要把dict重建回Document对象
from app.core.config import settings # 读redis_url

CACHE_TTL=10*60
# 缓存存活10分钟(600秒) 
# 手册约定:缓存短(分钟级),JWT长(6小时),别搞混 
# 写成乘法表达式而不是600,让人一眼看懂"10分钟"
# 10分钟也是兜底保鲜期:即使失效逻辑漏了,旧答案最多活10分钟

_client=_redis.from_url(
  settings.redis_url, # 从配置模块获取Redis连接URL
  decode_responses=True, # 开启响应解码,Redis返回的字节串自动转str,省去手动decode
)
# 模块级单例:进程启动时创建一次,全进程复用连接池,和_bm25_redis_client同一模式

def _normalize(question:str)->str:
    """问题归一化:去首尾空白+转小写+合并连续空白。
    目的:提高命中率——" PreparedRequest   在 哪 "->"preparedrequest 在 哪"
    归一化后是同一个key,不会因为多打了个空格就穿透缓存去打rerank API。"""
    return " ".join(question.strip().lower().split())
    # question.strip():去首尾空白 
    # .lower():统一小写,英文标识符大小写不同视为同一问题 
    # .split():不带参数的split按任意连续空白切分,自动丢弃空串 
    # " ".join(...):用单个空格重新拼接,等效于"合并所有连续空白为一个空格"

def _cache_key(repo_id:int|None,collection:str,question:str)->str:
    """拼缓存key,两种命名空间:
    retrieve:repo:{repo_id}:{指纹}——推荐路径,请求带了repo_id
    retrieve:coll:{collection}:{指纹}——兼容路径,只给了collection名
    为什么key里必须带仓库标识(手册点名)?
    同一个问题"Session类在哪定义",问requests和问flask答案完全不同,
    key里不带repo_id/collection就会跨仓库串数据——A仓库的答案被B仓库的提问命中。"""
    digest=hashlib.sha256(
      _normalize(question).encode("utf-8") # sha256吃字节串,str先按utf-8编码(字符->字节)
    ).hexdigest()[:16]
    # hexdigest():拿到64位十六进制字符串 
    # [:16]:只取前16位(64bit),本项目规模下碰撞概率可忽略,key更短可读
    if repo_id is not None:
        return f"retrieve:repo:{repo_id}:{digest}"
    return f"retrieve:coll:{collection}:{digest}"

def get_cached_docs(repo_id:int|None,collection:str,question:str)->list[Document]|None:
    """读缓存。命中返回Document列表;未命中或出错返回None。
    铁律:缓存读失败绝不能搞挂问答主链路——Redis挂了的正确表现是"变慢",不是"报错"。"""
    key=_cache_key(repo_id,collection,question) # 先拼好key,命中日志要打印它
    try:
      raw=_client.get(key) # 根据key从Redis客户端读取缓存对象
      if raw is None:
        return None # 未命中:返回None,调用方走完整检索流程
      data=json.loads(raw) # 把JSON字符串还原成字典:{"docs":[{content,metadata},...]}
      docs=[
        Document(page_content=d["content"],metadata=d["metadata"])
        for d in data["docs"]
      ] # 把每个dict重建回Document对象,检索下游(prompt拼接/sources)只认Document
      print(f"[cache]命中,key={key},docs={len(docs)}条") # 验收时靠这行日志确认缓存生效
      return docs
    except Exception as e:
      # Redis连不上/JSON损坏/字段缺失:全部按未命中处理,只留一条日志
      print(f"[cache]读取失败,按未命中处理:{e}")
      return None

def set_cached_docs(repo_id:int|None,collection:str,question:str,docs:list)->None:
    """写缓存。同样:写失败只打印,不抛异常。
    注意:空结果(docs=[])也会缓存——"查无资料"也是合法答案,
    重复问一个没答案的问题同样该快。"""
    try:
      data={
        "docs":[
          {"content":d.page_content,"metadata":d.metadata}
          for d in docs
        ]
      } # Document拆成纯dict,JSON才能序列化
      _client.set(
        # _client.set:往Redis写入缓存,格式为(key,value,ex=秒数)
        _cache_key(repo_id,collection,question),
        json.dumps(data,ensure_ascii=False), # ensure_ascii=False:中文不转义,Redis里可读
        ex=CACHE_TTL, # ex=秒数:10分钟后Redis自动删除,无需手动清理
      )
    except Exception as e:
      print(f"[cache]写入失败(不影响本次回答):{e}") # 缓存写失败不影响主链路,只打印日志

async def invalidate_repo_cache(redis_client,repo_id:int,collection:str)->int:
    """重新解析成功后,清掉该仓库两种命名空间的所有旧缓存。
    redis_client:传入worker里ctx["redis"](arq的异步客户端),本函数只在worker里用。
    返回删除的key条数,供worker打日志。"""
    deleted=0 # 计数器:统计删了几条
    for pattern in (f"retrieve:repo:{repo_id}:*",f"retrieve:coll:{collection}:*"):
      # 所有形似"retrieve:repo:{repo_id}:*"或"retrieve:coll:{collection}:*"的key都删
      # 两种命名空间都清:同一个仓库可能被repo_id路径和collection路径都问过
      async for key in redis_client.scan_iter(match=pattern):
        # scan_iter:SCAN命令的迭代器封装,分批渐进遍历匹配的key 
        # 为什么不用KEYS命令?KEYS会一次性扫全库并阻塞Redis主线程 
        # 生产环境KEYS是事故级操作;SCAN分批进行,不影响其他客户端
        await redis_client.delete(key) # 删除key对应的缓存对象
        deleted+=1
    return deleted
