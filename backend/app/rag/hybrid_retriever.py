import re # 正则表达式模块
import jieba # 中文分词模块
import httpx # 导入httpx模块用于调用硅基流动重排模型
import json # 标准库模块名，用于序列化/反序列化JSON字符串
import redis as _redis # 导入redis模块用于连接Redis数据库，_redis表示私有模块名，避免被外部访问

from langchain_core.documents import Document # 重建BM25时要Document对象
from app.core.config import settings # 导入配置模块用于获取重排序模型路径
from app.rag.answer_guard import extract_symbol, has_definition # 定位问句抽符号/判定义
from langchain_community.retrievers import BM25Retriever # 导入BM25Retriever类

CANDIDATE_K=80 # 每路初检多少条候选(加60%,粗筛多捞候选避免好块被挤掉)
RERANK_TOP_N=6 # 重排后最终喂给LLM的条数(加50%,直接提高context_recall)

# 预编译正则：匹配编程语言标识符（snake_case / CamelCase 整词）
_IDENT_RE=re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 问题里显式点名的文件名,如 "answer_guard.py 里有哪些函数"
# 这类"整文件概览"问题,定义块本身词频不高,向量和BM25都容易只召回引用它的地方,
# 所以单独按文件名捞一路候选
_FILE_RE=re.compile(r"[A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|json|toml|sql|sh|css)")
# [A-Za-z] 表示匹配大写字母或小写字母
# [0-9] 表示匹配数字
# _ 表示匹配下划线
# 开头_下划线表示数据私有，避免被外部访问
# re.compile()表示预编译正则表达式，提高匹配效率，即先编译正则表达式，再匹配文本
# * 表示匹配0个或多个前面的字符

# 定义_tokenize分词器函数
def _tokenize(text:str)->list[str]:
  """BM25分词器:标识符整词保留,同时拆分驼峰命名,中文交给jieba。"""
  text=re.sub(r'([a-z])([A-Z])',r'\1 \2',text)
  # 把驼峰命名拆分成多个单词,如getUserId->get user id
  # re.sub()表示替换所有匹配的子字符串，返回新的字符串
  # \1 表示匹配到的第一个子字符串, \2 表示匹配到的第二个子字符串
  tokens=[w.lower() for w in _IDENT_RE.findall(text)] 
  # 代码标识符：整词、统一小写
  # w.lower()表示将每个匹配到的标识符转换为小写
  # findall()表示返回所有匹配的子字符串列表
  no_code=re.sub(r"[A-Za-z0-9_]+"," ",text) 
  # 把文本中的英文/数字/下划线替换成空格:" "，剩下的主要是中文
  # re.sub()表示替换所有匹配的子字符串，返回新的字符串
  tokens.extend(t for t in jieba.lcut(no_code) if t.strip())
  # jieba.lcut()表示将文本进行中文分词，返回分词结果的列表(只保留中文分词)
  # t.strip()表示去掉每个分词结果的首尾空格,非空即为真
  # tokens.extend()表示将分词结果添加到tokens列表中
  # 设计目的:代码问答的查询是「中文 + 标识符」混合体,
  # 比如「get_user_by_id 在哪定义」。
  # 一套分词策略吃不下两种内容,
  # 所以英文走正则整词、中文走jieba,拼起来两头兼顾。
  return tokens
# rrf_fuse融合函数
def _is_def_of(symbol:str,ident:str)->bool:
  """判断 symbol 是否是 ident 的定义块,兼容两种索引格式。
  - AST 切分产出的裸符号名: "request" / "Session (large)"
  - 无 grammar 时的简单分割: "def request(" / "class Session(SessionRedirectMixin):"
  为什么要兼容?服务器缺 tree-sitter 语法包时会退化成简单分割, symbol 变成签名首行,
  此时按 symbol == "request" 过滤一条都查不到,精确召回会静默失效。
  """
  s=str(symbol or "").replace("(large)","").strip().lower()
  # 去掉超长块后缀和首尾空白,统一小写,保证大小写不影响判断
  i=ident.lower()
  if s==i:
    return True
  # 裸符号名命中
  return s.startswith((f"def {i}(",f"async def {i}(",f"class {i}("))
  # 签名首行命中:startswith 传元组=命中任意一个前缀即 True
  # class 后一定跟左括号(继承或空参数),所以 "class Session(" 能覆盖 "class Session(SessionRedirectMixin):"

def _const_pattern(name:str)->str:
  """常量定义的正则,允许名字前缀。

  为什么不能只匹配"NAME=":用户追问时常写缩写 TTL,
  代码里定义的是 CACHE_TTL=10*60,精确匹配会漏掉,
  导致模型只知道"有 TTL"却拿不到 600 这个数值。
  """
  return rf"^[ \t]*[A-Za-z_]*{re.escape(name)}\s*="

def _search_file_blocks(question:str,bm25:BM25Retriever)->list:
  """按问题里点名的文件名召回该文件的块。

  为什么需要:用户问"answer_guard.py 里有哪些函数"时,
  真正要的是这个文件内部的块,但向量/BM25按语义打分,
  常常只召回 import 或调用它的地方(测试文件、路由文件)。
  这里用文件名本身跑一次 BM25,再按 file_path 后缀过滤,
  零额外网络请求,只多一次内存检索。
  """
  match=_FILE_RE.search(question)
  if not match:
    return []
  name=match.group(0).lower()
  try:
    hits=list(bm25.invoke(name))
  except Exception as exc:
    print(f"[hybrid] 文件名召回失败:{exc}")
    return []
  picked=[d for d in hits if str(d.metadata.get("file_path","")).lower().endswith(name)]
  print(f"[hybrid] 文件候选({name}):{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in picked]}")
  return picked[:8]

def rrf_fuse(ranked_list:list[list],k:int=60)->list: 
    # list[list]是因为后面传入的两条路的检索结果都是列表,每个元素是一个Document对象
    # k=60的作用:压平曲线,避免排名过低的文档对融合结果的影响过大
    """RRF(Reciprocal Rank Fusion)倒数排名融合。
    为什么用它?向量的分数(0~1 余弦)和 BM25 的分数（无上界整数）
    根本没法直接相加。RRF 只看排名、不看分数，天然解决了量纲不一致。
    公式:score = Σ 1/(k + rank),k=60 是原论文经验值。"""
    scores={} # key->累计融合分
    first_seen={} #key->第一次见到的那个Document(两路重复命中时只留一份)
    for lst in ranked_list: 
      # 遍历两条路的检索结果
      for rank,item in enumerate(lst,start=1):
      # start=1表示从排名1开始计数,而不是从0开始，例如
      # 0-甲 -> 1-甲
      # 1-乙 -> 2-乙，符合人类看排名的习惯
        key=item.metadata.get("file_path")+":"+str(item.metadata.get("start_line"))
        # 每个Document的key是文件路径+起始行号,确保唯一性
        scores[key]=scores.get(key,0)+1/(k+rank)
        # 累计融合分,1/(k+rank)是RRF公式
        # get(key,0)表示如果key不存在,则返回0,避免KeyError
        # 每根据key找到一次,就累计一次融合分，并加入scores字典
        first_seen.setdefault(key,item) 
        # 只在该key第一次出现时记录文档对象,实现去重:两路都命中的块只保留一份。
        # setdefault()表示如果key不存在,则赋值为item,否则不改变item
    ranked=sorted(scores.items(),key=lambda x:x[1],reverse=True)
    # scores.items()返回一个字典视图,包含所有键值对
    # sorted()对键值对进行从高到低排序,返回一个列表,每个元素是一个元组(key,score)
    # x[1]表示取每个元组的第二个元素(即score)
    # reverse=True表示按降序排序,即从高到低
    return [(first_seen[key],score) for key,score in ranked]
    # 遍历排序后的列表,返回一个列表,每个元素是一个元组[文档对象,融合分]
    # first_seen[key]表示根据key获取第一次见到的那个Document对象
    # score表示当前融合分

# build_bm25建BM25索引
def build_bm25(chunks:list[dict])->BM25Retriever:
    """用AST切分产物(content+metadata字典列表)在内存里建BM25索引。
    把symbol拼进索引文本,让BM25能匹配到类名/函数名本身。"""
    bm25=BM25Retriever.from_texts(
      # BM25Retriever类的from_texts方法,用于从文本列表构建BM25索引
      # texts参数是文本列表,每个元素是一个字符串,用于构建索引
      # metadata参数是元数据列表,每个元素是一个字典,用于存储每个文本的元数据
      texts=[f"{c['metadata'].get('file_path','')} {c['metadata']['symbol']} {c['content']}" for c in chunks],
      # 把文件路径、符号symbol、内容拼进索引文本,让BM25能匹配到类名/函数名本身
      metadatas=[c["metadata"] for c in chunks], 
      preprocess_func=_tokenize, 
      # 告诉BM25Retriever使用_tokenize分词器,因为BM25默认是空格分词
    )
    bm25.k=CANDIDATE_K # 默认只返回4条,改成CANDIDATE_K(当前50条)
    return bm25
    # 返回BM25Retriever对象,用于后续检索

# 定义rerank重排序函数
def rerank(query:str,docs:list,top_n=RERANK_TOP_N)->list:
    """调用硅基流动重排模型,按"query 与文档的真实相关性"对候选块精排出top_n。
    
    为什么需要rerank?
    BM25和向量检索各有偏科:
    - BM25强于关键词匹配(精确命中变量名、函数名)
    - 向量强于语义相似("登录"能匹配"sign in")
    但两者都只是"粗排"——真正判定"哪个块最能回答这个问题"还得让Cross-Encoder重排模型来做。
    它把query和每个文档拼成一对,逐对打分,精度远高于双塔的向量相似度。
    
    截断策略:每个文档只送前1400字符(注释里之前写1500是笔误,代码以1400为准)。
    为什么要截断?rerank API按token数计费,且单次请求有上限。
    一个代码块动辄几千上万字符,全送进去既慢又贵,前1400字符通常已包含函数签名和核心逻辑,足够重排模型判断相关性。
    
    降级兜底:重排失败(HTTP错误/超时)时回退到RRF融合顺序取前top_n,保证问答链路不崩。
    为什么降级用RRF顺序?RRF融合已经把两路排序的信息都综合了,是"没rerank情况下能拿到的最好排序"。"""
    if not docs: # docs为空列表=RRF阶段就没找到任何相关文档,直接返回空,不用调API
        return []
    
    # httpx.post:同步HTTP POST请求,往硅基流动的/v1/rerank端点发请求
    # 为什么用httpx而不是requests?httpx支持http2、连接池更好,且项目里embedding已经用httpx了,统一依赖
    resp=httpx.post(
      f"{settings.embedding_base_url}/rerank",
      # f-string拼URL:settings.embedding_base_url通常是"https://api.siliconflow.cn/v1"
      # 拼完变成"https://api.siliconflow.cn/v1/rerank"——符合硅基流动兼容OpenAI风格的API路径
      headers={"Authorization":f"Bearer {settings.embedding_api_key}"},
      # HTTP请求头里必须带Authorization,格式是"Bearer " + API密钥字符串
      # 这是OpenAI兼容API的通用认证方式,硅基流动/阿里云DashScope等都支持
      json={
        "model":settings.rerank_model, # 重排序模型名,从.env读,默认是BAAI/bge-reranker-v2-m3
        "query":query, # 用户的原始问题,重排模型会拿它和每个文档配对打分
        "documents":[
          # 关键:给每个文档拼一个元数据前缀,格式="[文件: xxx 符号: yyy]\n 正文前1400字"
          # 为什么要拼?纯正文LLM看不到"这个块来自哪个文件、是什么函数",可能把测试桩当成真实实现
          # 截断[:2000]:控制token消耗,代码块前2000字符通常已包含函数签名和核心逻辑
          f"[文件: {d.metadata.get('file_path')} 符号: {d.metadata.get('symbol')}]\n{d.page_content[:2000]}"
          for d in docs
        ],
        "top_n":top_n, # 告诉rerank模型"我只想要相关性最高的前N条",API返回量更少更快
      },
      timeout=30, # 网络超时30秒。rerank是HTTP调用,比embedding慢(因为要逐对打分),给足30秒
      # 所有跨网络调用必须带timeout——否则对端挂了会无限阻塞整个FastAPI事件循环
    )
    try:
      resp.raise_for_status()
      # httpx.Response.raise_for_status():如果HTTP状态码是4xx/5xx(比如401密钥失效、429限流、500服务挂了),
      # 就抛httpx.HTTPStatusError异常。2xx(200~299)不抛,继续往下走。
      # 为什么要try/except?rerank是精排环节,失败了不该让整个问答崩掉——降级返回RRF顺序就行。
    except httpx.HTTPStatusError as e:
      # 捕获所有HTTP错误状态码(401/429/500等)
      # e.response.status_code:比如429就是被限流了,401就是API key无效
      print(f"[rerank]重排失败(HTTP {e.response.status_code}),降级用RRF顺序")
      # 降级方案:直接返回RRF融合后的docs前top_n条——这是没rerank时能拿到的最好排序
      # 注意:docs此时是RRF排好序的,前top_n就是RRF认为最好的top_n
      return docs[:top_n]
    
    # 正常路径:解析rerank响应JSON
    # 硅基流动rerank API返回格式:{"results":[{"index":0,"score":0.95},...]}
    # index:对应我们传入的documents数组的下标,score:相关性分数(越高越相关)
    results=resp.json()["results"]
    # resp.json():httpx内置方法,把响应体字符串按JSON解析成Python dict
    # ["results"]:取出results数组,每个元素有index和score字段
    
    # 按index映射回原始docs,得到rerank排序后的文档列表
    # results本身已按score降序排好(rerank模型会自动返回排好序的列表)
    return [docs[r["index"]] for r in results]
    # 列表推导式:遍历results中每个元素r,取r["index"],再用这个index从docs里取对应的Document对象
    # 举例:results=[{index:5,score:0.9},{index:2,score:0.8}]
    # 返回[docs[5], docs[2]]——rerank认为第6个文档最相关,第3个次之

# =====逐字精确检索=====
# 解决什么问题:用户贴一整句原文问"这句话在哪",向量检索看重语义、
# BM25 看重词频,都可能把这句所在的块排到 30 名开外,重排后直接丢掉。
# 做法:从问题里抽"疑似原文片段",直接去 pgvector 表里 ILIKE 匹配,
# 命中块作为最高优先级候选,绕开向量/BM25 排序。
_QUOTED_RE=re.compile(r"[「“\"']([^」”\"']{6,})[」”\"']")

def _literal_phrases(question:str)->list[str]:
  """从问题里抽取可逐字匹配的片段。

  两类来源:
  1) 引号/书名号里的内容(≥6 字),用户明确在引用原文;
  2) 按句末标点切出的长句(≥14 字),对应"把整句话粘贴进来问在哪"的场景。
  限制最多 3 条、单条 160 字:既控制 SQL 次数,也避免 ILIKE 扫太长的串。
  """
  text=question.strip()
  found=list(_QUOTED_RE.findall(text))
  # 注意不按分号切:目标句子本身可能含";",切碎后反而只匹配到半句
  for seg in re.split(r"[。!?!?\n]",text):
    seg=seg.strip()
    if len(seg)>=14:
      found.append(seg)
  cleaned=[]
  for c in found:
    c=re.sub(r"\s+"," ",c).strip(" ,，;；:：。")
    if len(c)>=6:
      cleaned.append(c[:160])
  # dict.fromkeys 去重且保持原顺序;最多 3 条
  return list(dict.fromkeys(cleaned))[:3]

def _search_literal_text(question:str,collection:str)->list:
  """在 langchain_pg_embedding 里做逐字匹配,返回命中的 Document。

  为什么直接写 SQL 而不走 store.similarity_search:
  PGVector 的检索只有"向量距离 + metadata filter",没有"正文包含"能力;
  而 langchain_pg_embedding.document 就是原始块文本,ILIKE 最直接。
  collection_id 通过 langchain_pg_collection.name 关联,只查当前仓库。
  """
  if not collection:
    return []
  phrases=_literal_phrases(question)
  if not phrases:
    return []
  # SQLAlchemy 的 URL 带驱动后缀(postgresql+psycopg:// / postgresql+asyncpg://),
  # psycopg.connect 只认标准 postgresql://,这里统一剥掉驱动段
  dsn=re.sub(r"^postgresql\+[a-z0-9_]+://","postgresql://",settings.database_url)
  # 同步连接串:hybrid_search 本身是同步函数,调用方已放进线程池
  docs_by_key={}
  best_len={} # key -> 命中的最长片段长度,越长越能证明"就是这句原文"
  hit_count={} # key -> 命中片段数
  try:
    import psycopg # langchain-postgres 的依赖,这里局部导入避免影响其他模块
    with psycopg.connect(dsn,connect_timeout=8) as conn:
      with conn.cursor() as cur:
        # 按长度从长到短查:长片段更具体,优先占位;
        # 短引号片段(如「自带 Key」)容易命中无关文件,只作为兜底
        for phrase in sorted(phrases,key=len,reverse=True):
          # 关键:先去掉块内所有空白(换行/缩进/多余空格)再比,问题侧同样去空白。
          # 源码里的整句常被格式化拆成多行(例:"...LLM Key;"换行+"只浏览和提问..."),
          # 用户粘贴的是同一行;只压成单空格仍会在换行处多一个空格,
          # 全删空白才能让两边严格对齐。
          # 用 [[:space:]] 而不是 \\s :标准 SQL 字符串里反斜杠无需转义,跨库更稳
          cur.execute(
            """select e.document,e.cmetadata::text
               from langchain_pg_embedding e
               join langchain_pg_collection c on e.collection_id=c.uuid
               where c.name=%s
                 and regexp_replace(e.document,'[[:space:]]','','g') ilike %s
               limit 8""",
            (collection,"%"+re.sub(r"\s+","",phrase)+"%"),
          )
          for doc,meta_raw in cur.fetchall():
            meta=json.loads(meta_raw)
            key=(meta.get("file_path"),meta.get("start_line"))
            if key not in docs_by_key:
              docs_by_key[key]=Document(page_content=doc,metadata=meta)
            best_len[key]=max(best_len.get(key,0),len(phrase))
            hit_count[key]=hit_count.get(key,0)+1
    # 先按"命中的最长片段"降序,再按命中片段数降序:
    # 整句命中的块排在只命中短引号片段的块前面,置顶时才不会被噪声挤掉
    docs=sorted(
      docs_by_key,
      key=lambda k:(-best_len.get(k,0),-hit_count.get(k,0)),
    )
    docs=[docs_by_key[k] for k in docs]
    print(f"[hybrid] 逐字命中:{len(docs)}条,片段={phrases},"
          f"最长匹配={[(k[0],k[1],best_len[k]) for k in docs_by_key]}")
  except Exception as exc:
    # 逐字检索只是增强路,失败不能拖垮整个问答
    print(f"[hybrid] 逐字检索失败:{exc}")
    docs=[]
  return docs

# =====界面元素定位检索=====
# 解决"某个按钮/弹窗/页面在哪"这类问题:
# 它们没有可粘贴的原文,也没有标识符,向量会被"删除"语义最浓的仓库删除逻辑带偏,
# 而正确代码往往藏在某个超大组件的 JSX 片段里。
# 做法:识别界面名词后,直接在前端组件文件里按"问题实词命中数"挑候选,
# 再交给 rerank 定序——不做强制置顶,避免猜错时把错误文件塞给用户。
_UI_NOUN_RE=re.compile(r"按钮|弹窗|对话框|输入框|下拉|菜单|标签|图标|组件|样式|页面|区域")
# 疑问词/虚词:匹配块时它们只会带来噪声
_UI_STOP={"的","了","是","在","哪","哪里","哪个","什么","怎么","如何","为什么",
          "文件","代码","行号","位置","请","问","一下","这个","那个","还有","以及"}

def _ui_words(question:str)->list[str]:
  """抽取用于匹配界面块的实词,并去掉界面名词本身。

  为什么去掉"按钮"两字:代码里写的是 <button>,正文不会出现"按钮",
  留着它只会让所有块都得 0 分。
  """
  parts=[w for w in jieba.lcut(question) if len(w)>=2 and w.strip() not in _UI_STOP]
  parts=[w for w in parts if not _UI_NOUN_RE.fullmatch(w)]
  return list(dict.fromkeys(parts))[:6]

def _search_ui_blocks(question:str,collection:str)->list:
  """按实词命中数,从前端组件的 JSX 块里挑界面元素候选。"""
  if not collection or not _UI_NOUN_RE.search(question):
    return []
  words=_ui_words(question)
  if not words:
    return []
  dsn=re.sub(r"^postgresql\+[a-z0-9_]+://","postgresql://",settings.database_url)
  scored=[]
  try:
    import psycopg
    with psycopg.connect(dsn,connect_timeout=8) as conn:
      with conn.cursor() as cur:
        # 只在带 className 的前端块里找:没有 className 的多半是逻辑代码,
        # 不会出现"按钮长什么样"这类答案
        cur.execute(
          """select e.document,e.cmetadata::text
             from langchain_pg_embedding e
             join langchain_pg_collection c on e.collection_id=c.uuid
             where c.name=%s
               and e.cmetadata->>'file_path' like %s
               and e.document like %s
             limit 400""",
          (collection,"frontend/%","%className=%"),
        )
        for doc,meta_raw in cur.fetchall():
          # 命中实词数就是相关度:例如"历史会话的删除按钮在哪"里
          # 同时出现"历史""会话""删除"的块明显比只含"删除"的更可能就是答案
          score=sum(1 for w in words if w in doc)
          if score<2:
            continue
          scored.append((score,Document(page_content=doc,metadata=json.loads(meta_raw))))
    scored.sort(key=lambda x:(-x[0],len(x[1].page_content)))
    docs=[d for _,d in scored[:5]]
    print(f"[hybrid] 界面候选:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in docs]}"
          f" 实词={words}")
  except Exception as exc:
    print(f"[hybrid] 界面检索失败:{exc}")
    return []
  return docs

# =====符号定义精确召回=====
# 解决"X 在哪定义"答不准的问题:向量/BM25 往往把"使用了 X 的块"排到前面,
# 而真正的定义块(尤其模块级常量)symbol 元数据是所在块的第一行(如 "import re # ..."),
# 按 symbol 过滤永远捞不到,于是模型只能看到"两处使用",答不出定义位置。
# 做法:直接从库里按名字粗筛,再用 answer_guard 的行首定义正则确认,确认的置顶喂给模型。
def _search_definition(question:str,collection:str)->list:
  symbol=extract_symbol(question)
  # extract_symbol:只在"X 在哪定义"这类问句返回符号名,其他问题返回 None(不产生额外查询)
  if not symbol or not collection:
    return []
  name=re.escape(symbol.split(".")[-1])
  dsn=re.sub(r"^postgresql\+[a-z0-9_]+://","postgresql://",settings.database_url)
  try:
    import psycopg
    with psycopg.connect(dsn,connect_timeout=8) as conn:
      with conn.cursor() as cur:
        # 先用名字粗筛(最多 80 条),symbol 里含这个名字的排前面,短块排前面
        # 精确判定放在 Python 里做:PG 的 ARE 语法和 Python re 不完全一致,
        # 复用 answer_guard.has_definition 才能保证"和拒答判定同一套标准"
        cur.execute(
          """select e.document,e.cmetadata::text
             from langchain_pg_embedding e
             join langchain_pg_collection c on e.collection_id=c.uuid
             where c.name=%s and e.document ~* %s
             order by (e.cmetadata->>'symbol' ~* %s) desc,length(e.document) asc
             limit 80""",
          (collection,name,name),
        )
        rows=cur.fetchall()
    docs=[Document(page_content=doc,metadata=json.loads(meta)) for doc,meta in rows]
    docs=[d for d in docs if has_definition([d],symbol)]
    # 测试文件里的"定义"多半是测试辅助函数,真定义优先;同优先级取更短的块
    docs.sort(key=lambda d:(
      str(d.metadata.get("file_path","")).replace("\\","/").startswith(("tests/","test_")),
      len(d.page_content),
    ))
    docs=docs[:3]
    print(f"[hybrid] 定义候选:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in docs]}")
    return docs
  except Exception as exc:
    # 定义召回只是增强路,失败退回原有检索顺序
    print(f"[hybrid] 定义召回失败:{exc}")
    return []


# hybrid_search混合检索
def hybrid_search(question:str,store,bm25:BM25Retriever,mode:str="full")->list:
    """混合检索全流程:双路初检->RRF融合->重排精排。

    mode:
      "vector" 仅向量检索(top RERANK_TOP_N),做消融基线
      "hybrid" 多路召回 + RRF 融合 + 精确/非测试排序,不做重排
      "full"   完整链路(默认值,线上行为不变):以上全部 + rerank 精排
    """
    if mode not in ("vector","hybrid","full"):
        raise ValueError(f"unknown mode: {mode}")
    if mode=="vector":
        return store.similarity_search(question,k=RERANK_TOP_N)
    # store是向量数据库,用于存储和检索向量
    # bm25是BM25索引,用于快速检索相关文档
    vec_docs=store.similarity_search(question,k=CANDIDATE_K)
    # store.similarity_search()方法->pgvector按距离从小到大返回与之最相似的CANDIDATE_K条向量
    kw_docs=bm25.invoke(question)
    # bm25.invoke()方法->按BM25分从高到低返回，同样是排好序的。
    print ( f"[hybrid] vec候选: {[d.metadata.get( 'symbol' ) for d in vec_docs]} " ) 
    # 调试日志:打印vec阶段候选symbol,排查召回问题时使用
    print ( f"[hybrid] kw候选: {[d.metadata.get( 'symbol' ) for d in kw_docs]} " )
    # 调试日志:打印kw阶段候选symbol,排查召回问题时使用

    # 文件级召回:问题里点名了某个文件,就按文件名把该文件的块捞一路进来。
    # 不点名文件时返回空,不产生额外开销
    file_docs=_search_file_blocks(question,bm25)
    _file_match=_FILE_RE.search(question)
    file_name=_file_match.group(0).lower() if _file_match else None

    # 第三路(前置):逐字原文召回
    # 例如用户粘贴"提交新仓库需要先在右上角…"问这句话在哪:
    # 这类问题向量/BM25都不可靠,必须靠正文精确包含关系来保证召回
    exact_text_docs=_search_literal_text(question,getattr(store,"collection_name",""))
    print(f"[hybrid] 逐字候选:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in exact_text_docs]}")

    # 第四路(前置):界面元素召回
    # 用户问"某个按钮/弹窗/页面在哪"时,按问题实词在前端 JSX 块里挑候选;
    # 不问界面元素时该路返回空,不产生额外开销
    ui_docs=_search_ui_blocks(question,getattr(store,"collection_name",""))

    # 第五路(前置):符号定义精确召回
    # "X 在哪定义"这类问题必须看到定义块本身,否则模型只能答"哪里用到了 X"
    def_docs=_search_definition(question,getattr(store,"collection_name",""))

    # 第三路:符号精确召回
    # 解决"高频词"问题:command 在tests里出现上百次,IDF被稀释,定义块进不了前50
    # 做法:从query里抽标识符,直接按symbol字段过滤查库,绕开打分
    ids=[i for i in _IDENT_RE.findall(question) if len(i)>=3]
    # 从问题中提取长度>=3的标识符(短词如in/of无意义)
    stop={"click","def","class","the","and","for","import","return","self","test"}
    # 停用词:仓库名、语法关键字,按它们过滤要么查不到、要么噪音大
    ids=[i for i in dict.fromkeys(ids) if i.lower() not in stop][:3]
    # dict.fromkeys()去重,最多取3个,控制embedding调用次数

    # 第三路补强:用标识符单独跑一次BM25。
    # 为什么需要?symbol过滤只对"裸符号名"有效;若索引是简单分割产出的
    # (symbol 形如 "def request("),filter={"symbol":"request"} 一条都匹配不到。
    # BM25索引文本里拼了 symbol,所以用标识符单独检索能捞回这类定义块。
    exact_bm25=[]
    for ident in ids:
        try:
            exact_bm25.extend(bm25.invoke(ident))
        except Exception as e:
            print(f"[hybrid] ident BM25召回失败({ident}):{e}")

    exact_docs=[]
    for ident in ids:
      # 同一符号在库里可能有两种symbol:普通块,和超长块(切分时加了" (large)"后缀)
      # 真实实现往往就是那个超长块,两种都要查,否则只能捞到overload桩(函数体只有一个 ...)
      for sym in (ident,f"{ident} (large)"):
        try:
          exact_docs.extend(store.similarity_search(question,k=12,filter={"symbol":sym}))
          # filter={"symbol":sym}:只在symbol等于该符号名的块里做向量检索
          # k=12:同一符号(如overload桩)可能有多条,给足12条
        except Exception as e:
          print(f"[hybrid] exact召回失败({sym}):{e}")
          # 单路失败不影响主链路,继续用vec+bm25
    print(f"[hybrid] exact候选:{[d.metadata.get('symbol') for d in exact_docs]}")
    # 调试日志:确认到底捞到了哪些定义块

    fused=rrf_fuse([vec_docs,kw_docs,exact_docs,exact_bm25,exact_text_docs,ui_docs,def_docs,file_docs])
    # 两路列表丢进RRF融合函数,对应list[list]的传参格式,进行融合去重+按融合分排序
    if not fused: # 两路都没捞到,直接返回空,不拿空列表去调重排接口 return []
        return []
    # 精确匹配加成:query中提取标识符,symbol包含它的块额外加分
    identifiers=re.findall(r'[A-Za-z_][A-Za-z0-9_]*',question)
    # 从query中提取所有标识符,如get_user_by_id
    boosted=[] # 存储额外加分的文档
    for doc,score in fused: # 遍历融合后的文档列表
      symbol=doc.metadata.get('symbol','') # 从文档元数据中提取symbol,默认值为空字符串
      path=str(doc.metadata.get('file_path','')).lower()
      # 定义块优先:symbol 就是"该标识符的定义"时加更多分。
      # 兼容两种索引格式:裸符号名("request (large)")和签名首行("def request(")。
      if any(_is_def_of(symbol,idf) for idf in identifiers if len(idf)>2):
        boosted.append((doc,score+2.0)) # 定义本体,最高优先级
      elif file_name and path.endswith(file_name):
        # 问题点名了这个文件:该文件内的块优先于"引用它的地方"
        boosted.append((doc,score+2.0))
      elif any(idf.lower() in symbol.lower() for idf in identifiers if len(idf)>2):
        # len(idf)>2 表示只考虑长度大于2的标识符,避免匹配到单字符的标识符(如in,of等)
        boosted.append((doc,score+1.0)) # 只是名字里含标识符,次优先级
      else:
        boosted.append((doc,score)) # 没有额外加分
    # 按加成后分数重排
    boosted.sort(key=lambda x:x[1],reverse=True)

    # 同(文件,符号)最多保留2条:避免重载桩(如4条command)占满候选池挤掉定义本体
    candidates=[]
    seen={} # 记录每个(文件,符号)已保留几条
    for doc,_ in boosted:
      key=(doc.metadata.get('file_path',''),doc.metadata.get('symbol',''))
      if seen.get(key,0)>=3: # 已保留3条,跳过
        continue
      seen[key]=seen.get(key,0)+1 # 计数+1
      candidates.append(doc)
      if len(candidates)>=30: # 放宽到30条,给更多候选进入重排的机会
        break
    print(f"[hybrid]RRF候选({len(candidates)}条):{[d.metadata.get('symbol') for d in candidates]}")
    # 调试日志检查

    # ---- 第一步:精排(改动的核心之一) ----
    # 原来这里写的是 n=len(candidates) if need_def else RERANK_TOP_N,
    # 只有问题里含"定义/实现/声明"才全量精排,其他问题只精排出前6条。
    # 现在改成:无论什么问题,都把全部候选送进rerank。
    # 为什么?rerank的top_n只决定"返回几条",送进去的文档都已经按token计费了,
    # 拿全量返回不额外花钱,但能保证下面"精确符号兜底"有一份完整的打分列表可用。
    if mode=="hybrid":
      # 消融对照组:不调重排接口,直接沿用 RRF+加成后的顺序
      result=list(candidates)
    else:
      result=rerank(question,candidates,top_n=len(candidates))
    # result:rerank的返回值,是一个list[Document],已按相关性从高到低排好序
    # rerank(...):上面定义的重排序函数,内部调硅基流动的/v1/rerank接口,失败会自动降级成RRF顺序
    # question:用户的原始问题,原样透传给重排模型做query
    # candidates:上一段(第228~237行)去重后的RRF候选列表,最多30条
    # top_n=len(candidates):返回条数=候选总数,等价于"把所有候选的打分顺序原样要回来"
    # len(...):Python内置函数,返回列表长度,即候选条数

    # ---- 第二步:提取"必须保留"的精确符号集合 ----
    exact_syms={i.lower() for i in ids}
    # exact_syms:集合(set),元素是小写后的标识符,查找是O(1),比list的in快得多
    # { ... for i in ids}:集合推导式,和列表推导式写法一样,只是用花括号产出set
    # i.lower():统一转小写,因为symbol元数据里可能是Session也可能是session
    # ids:第187~192行算出来的标识符列表(从问题里抽长度>=3、去掉停用词、最多3个),
    #     如"requests的request函数定义在哪个文件？"会抽到["requests","request"]
    #     注意它已经被精确召回那一路用过,这里复用它,不再重新抽一次

    # ---- 第三步:两个判定小函数 ----
    def _sym(x):
      # _sym:归一化符号名的辅助函数
      # 开头下划线表示"内部使用",外部不调它
      # x:入参,可能是"Session",也可能是"Session (large)",也可能是None
      return str(x).replace("(large)","").strip().lower()
      # str(x):元数据里的symbol可能是None,match对象也不是字符串,统一转成str防报错
      # .replace("(large)",""):AST切分时超长块会在symbol后加" (large)"后缀,
      #                          去掉它,才能和query里抽出来的纯标识符对齐
      # .strip():replace后会残留一个空格("Session (large)"->"Session "),去掉首尾空白
      # .lower():再统一小写,保证"Session"和"session"视为同一个符号

    def _is_exact(d):
      # _is_exact:判断这条文档的符号是不是"query里精确点名"的那几个
      # d:一条Document对象
      symbol=d.metadata.get("symbol","")
      return _sym(symbol) in exact_syms or any(_is_def_of(symbol,i) for i in exact_syms)
      # 前半段兼容裸符号名("request"),后半段兼容签名首行("def request(")
      # d.metadata:Document的元数据字典,由AST切分时写入(file_path/start_line/end_line/symbol)
      # d.metadata.get("symbol",""):取symbol,取不到就给空字符串(用[]会抛KeyError)
      # ... in exact_syms:集合成员判断,命中返回True
      # 返回值:True=这条块是{request/Session/...}之一的定义,必须优先保留

    def _is_test(d):
      # _is_test:判断这条文档是不是测试文件
      return str(d.metadata.get("file_path","")).replace("\\","/").startswith(("tests/","test_"))
      # .replace("\\","/"):Windows上路径是src\requests\...,Linux上是src/requests/...,
      #                     统一成正斜杠,后面前缀判断才跨平台一致
      # .startswith(("tests/","test_")):startswith接受元组=命中任意一个前缀即True,
      #                                  "tests/test_requests.py"和"test_xxx.py"都算测试文件

    # ---- 第四步:排序(精确符号优先,其次非测试文件) ----
    result.sort(key=lambda d:(0 if _is_exact(d) else 1,1 if _is_test(d) else 0))
    # result.sort(...):原地排序,直接改result,不产生新列表
    # key=lambda d:...:排序依据,对每条文档d计算一个元组,Python按元组逐位比较
    # (第一个键,第二个键):
    #   第一键 0 if _is_exact(d) else 1 -> 精确命中的排在前面(0<1)
    #   第二键 1 if _is_test(d) else 0 -> 测试文件排后面(非测试=0<测试=1)
    # sort是稳定排序:两个键都相等时,保持rerank原来的相关性顺序,不会打乱精排结果

    # ---- 第四步补:逐字命中强制置顶 ----
    # 为什么不只靠 RRF:逐字块通常没有 symbol、也不是定义块,
    # 在"精确符号优先"的排序里会被压后;它又是用户明确要的证据,
    # 所以直接提到 result 最前面,保证一定进入最终 top 6。
    if exact_text_docs:
      pinned=exact_text_docs[:3]
      pinned_keys={(d.metadata.get("file_path",""),d.metadata.get("start_line")) for d in pinned}
      result=[d for d in result if (d.metadata.get("file_path",""),d.metadata.get("start_line")) not in pinned_keys]
      result=pinned+result
      print(f"[hybrid] 逐字命中置顶:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in pinned]}")

    # 第四步补二:定义块强制置顶
    # 与逐字召回同理:定义块常常因为块太长、词频不高被 rerank 压后,
    # 但"X 在哪定义"的答案就在这一块里,压后等于让模型看不见定义。
    if def_docs:
      pinned=def_docs[:2]
      pinned_keys={(d.metadata.get("file_path",""),d.metadata.get("start_line")) for d in pinned}
      result=[d for d in result if (d.metadata.get("file_path",""),d.metadata.get("start_line")) not in pinned_keys]
      result=pinned+result
      print(f"[hybrid] 定义块置顶:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in pinned]}")

    # 第四步补三:点名文件的块强制置顶
    # "answer_guard.py 里有哪些函数"要的是文件内部,不是调用方;
    # 只置顶 2 块:文件头 + 第一个定义,其余交给加成后的相关性排序
    if file_docs:
      pinned=file_docs[:2]
      pinned_keys={(d.metadata.get("file_path",""),d.metadata.get("start_line")) for d in pinned}
      result=[d for d in result if (d.metadata.get("file_path",""),d.metadata.get("start_line")) not in pinned_keys]
      result=pinned+result
      print(f"[hybrid] 文件块置顶:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in pinned]}")

    # 第四步补四:常量定义块置顶
    # 问题里点了 CANDIDATE_K 这类常量,但定义它的模块头块常常因为
    # "函数体更像答案"被 rerank 压后,导致模型只答"取 CANDIDATE_K 条"却给不出数值。
    const_docs=[]
    if any(i.isupper() for i in ids):
      for d in candidates:
        content=d.page_content or ""
        if any(
          re.search(_const_pattern(i),content,re.MULTILINE)
          for i in ids if i.isupper()
        ):
          const_docs.append(d)
    if const_docs:
      pinned=const_docs[:1]
      pinned_keys={(d.metadata.get("file_path",""),d.metadata.get("start_line")) for d in pinned}
      result=[d for d in result if (d.metadata.get("file_path",""),d.metadata.get("start_line")) not in pinned_keys]
      result=pinned+result
      print(f"[hybrid] 常量块置顶:{[(d.metadata.get('file_path'),d.metadata.get('start_line')) for d in pinned]}")

    # 第四步补六:整文件型问题改为文件内取块
    # "answer_guard.py 里有哪些函数""最后一个函数返回什么"要的是文件内全貌,
    # 只置顶一两块不够:模型看不到函数顺序,会把别的文件的块当成"最后一个"。
    # 这里直接按 start_line 顺序取该文件的块(头 5 块 + 末块),不放其他文件进来,
    # 文件块少于 3 个时退回常规流程,避免上下文过窄
    _SCOPE_WORDS=("有哪些函数","几个函数","最后一个","最后那个","整个文件","文件里","全文")
    if file_name and any(word in question for word in _SCOPE_WORDS):
      all_docs=getattr(bm25,"docs",[])
      same_file=sorted(
        [d for d in all_docs if str(d.metadata.get("file_path","")).lower().endswith(file_name)],
        key=lambda d:int(d.metadata.get("start_line") or 0),
      )
      if len(same_file)>=3:
        scoped=same_file[:5]
        if same_file[-1] not in scoped:
          scoped=scoped+[same_file[-1]]
        result=scoped
        print(f"[hybrid] 整文件取块({file_name}):{[d.metadata.get('start_line') for d in scoped]}")

    # 第四步补五:常量数值回补
    # "候选池每路取多少条"这类问题,检索命中的是函数体,
    # 函数体只写 k=CANDIDATE_K,真正定义 80 的块在模块头。
    # 候选块里引用了哪些全大写常量,就按常量名把定义块捞回来置顶,
    # 否则模型只能答"取了 CANDIDATE_K 条",给不出数值。
    _NUM_WORDS=("多少","几","默认","上限","阈值","多大","几条")
    if any(word in question for word in _NUM_WORDS):
      const_refs=[]
      for d in candidates[:8]:
        for name in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b",d.page_content or ""):
          if name not in const_refs:
            const_refs.append(name)
      # 直接遍历全部块找"常量=值"的赋值块,而不是走 BM25 top-k:
      # 一个常量名往往在十几处被引用,定义块按词频排不进前几,
      # 遍历内存里的块列表(O(总数))比调检索更稳,成本也可忽略
      all_docs=getattr(bm25,"docs",[])
      # 问题里点名过的常量优先(如 TTL),其余按候选出现顺序补位
      ordered=[n for n in const_refs if n.lower() in question.lower()]
      ordered+=[n for n in const_refs if n.lower() not in question.lower()]
      for name in ordered[:5]:
        # 只认"= 数字"的定义:问"多少/几条"要的是数值,
        # 字符串型配置(API_KEY="sk-...")即使命中也没有信息量,反而挤掉真答案
        found=[d for d in all_docs
               if re.search(rf"^[ \t]*[A-Za-z_]*{re.escape(name)}\s*=\s*[0-9]",
                            d.page_content or "",re.MULTILINE)]
        if found:
          target=found[0]
          result=[d for d in result if (d.metadata.get("file_path"),d.metadata.get("start_line"))
                  !=(target.metadata.get("file_path"),target.metadata.get("start_line"))]
          result=[target]+result
          print(f"[hybrid] 常量回补置顶:{name} @ {target.metadata.get('file_path')}:{target.metadata.get('start_line')}")
          break

    # ---- 第五步:去重后截断 ----
    final=[]
    # final:最终要喂给LLM的文档列表,初始为空
    seen_keys=set()
    # seen_keys:已收录的(文件路径,起始行)集合,用来去重
    for d in result:
      # 按上面的顺序遍历rerank结果
      key=(d.metadata.get("file_path",""),d.metadata.get("start_line"))
      # key:文档的唯一标识=文件路径+起始行号,和rrf_fuse里用的key规则一致
      # get("start_line"):取不到返回None,不影响去重(同一文件同一行才是同一块)
      if key in seen_keys:
        # 已经收录过同一块,跳过
        continue
        # continue:跳过本次循环剩余语句,直接进入下一条文档
      seen_keys.add(key)
      # set.add(...):把key加入集合,标记"这条已收录"
      final.append(d)
      # list.append(...):把文档追加到最终列表尾部,保证顺序不变
      if len(final)>=RERANK_TOP_N:
        # RERANK_TOP_N:文件顶部的常量,当前是6,即最终喂给LLM的条数
        break
        # break:凑够条数就跳出循环,后面的文档直接丢弃
    print(f"[hybrid]rerank后:{[d.metadata.get('symbol') for d in final]}")
    # 调试日志:打印最终留下的都是哪些符号,排查时看这一行就知道定义块有没有进来
    return final
    # 返回精排+兜底后的文档列表,供pipeline.py拼context
    
# 注意:这里用同步redis,而不是async redis.asyncio。 
# 原因:API路由后续会改async def,但读取BM25原料这件事 
# 属于"启动时懒加载一次、后续全读内存缓存"的低频操作, 
# 用同步redis反而更简单,不用在路由里到处 await。 
# 如果你想全链路async,也可以换成redis.asyncio,逻辑一样。
_bm25_redis_client=_redis.from_url(
  # _bm25_redis_client:模块级变量名（全进程唯一一份，进程启动时创建一次），存储同步redis客户端实例
  # _redis.from_url():redis库的工厂方法，根据URL字符串自动解析host/port/db，创建连接池客户端
  settings.redis_url, # 从配置模块获取Redis连接URL
  decode_responses=True, # 开启响应解码,将Redis返回的字节字符串转换为str字符串
)

def _build_bm25_from_chunks ( chunks: list [ dict ] ) -> BM25Retriever: 
  """从AST切分的raw chunks字典列表重建BM25Retriever。
    和顶部的build_bm25逻辑完全一样,单独抽出来是为了复用。""" 
  return BM25Retriever.from_texts(
        texts=[f"{c['metadata']['symbol']} {c['content']}" for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
        preprocess_func=_tokenize,
    )

def _bm25_cache_hit(collection:str)->BM25Retriever|None:
    """三级查找:内存缓存->Redis原料->失败。
    成功时顺手回填内存缓存,下次就不用再读Redis了。"""
    # 第1级:内存命中(绝大多数请求走这里)
    cached=_bm25_memory.get(collection)
     # _bm25_memory.get():调用内存字典的get()方法
    if cached is not None:
        return cached
     # 如果collection键存在->对应的值（BM25Retriever对象）
     # 如果不存在->None（dict.get()默认返回None）

     # 第2级:从Redis取worker存的原料
     # key=f"bm25:{collection}",TTL7天(worker写的时候定的)
    raw=_bm25_redis_client.get(f"bm25:{collection}")
      # _bm25_redis_client.get():同步redis客户端的get()方法（对应Redis命令`GET`）
    if raw is None:
      # Redis里也没有->说明这个collection从未被worker ingest过
      # Redis里也没这个key->整个三级查找全miss
      return None
      # 向上返回None,让调用方（routes.py）知道
      # "这个collection从未被ingest"，由调用方决定报什么HTTP错误

    # 反序列化JSON->chunks字典列表 
    # worker存的是json.dumps(chunks),这里json.loads回来
    chunks=json.loads(raw)
      # json.loads():把JSON字符串还原成Python对象（list/dict）
    bm25=_build_bm25_from_chunks(chunks)
    # 从AST切分的raw chunks字典列表重建BM25Retriever
    bm25.k=CANDIDATE_K # 设置候选文档数量为CANDIDATE_K（当前50条）

    # 回填内存缓存:下次同collection的查询直接走第1级
    _bm25_memory[collection]=bm25
    # 向内存字典里写入一条记录。键是collection名，值是刚建的BM25Retriever
    print(f"[bm25]从Redis重建成功,collection={collection},chunks={len(chunks)}")
    # 为什么要回填?
    # 下次同一个collection的查询走第1级就能命中，不用再读Redis
    return bm25
    # 三级查找全部完成->返回最终可用的BM25Retriever

# 注意:这里用_bm25_memory作为内存缓存变量名, 
# 是因为routes.py原来用的是_bm25_cache,我们想和它解耦。 
# routes.py可以删掉它自己的_bm25_cache,统一调这个函数。
_bm25_memory:dict[str,BM25Retriever]={} # {}:表示初始时没有任何BM25索引被缓存进来
# dict[str, BM25Retriever]:键是字符串(collection名),值是BM25Retriever对象

def drop_bm25(collection:str)->None:
  """删除某个 collection 的 BM25 索引:内存缓存 + Redis 原料两层都要清。

  为什么只删 Redis 不够:本进程内存里的 _bm25_memory 还留着旧的索引对象,
  删仓库后继续提问会拿"已删除仓库"的代码做关键词召回,出现幽灵数据。
  为什么要删 Redis:_bm25_memory 是进程级缓存,重启/多 worker 时会回落读 Redis,
  不删 Redis 的话,另一个进程仍能把它重建出来。"""
  _bm25_memory.pop(collection,None)
  # pop(key,None):键不存在时返回 None 而不是抛 KeyError,删除不存在的仓库也不炸
  try:
    _bm25_redis_client.delete(f"bm25:{collection}")
    # delete:同步客户端删除,对应 Redis 的 DEL 命令
  except Exception as exc:
    # 清缓存失败不能让删除接口失败(数据库行已经删了),只打日志
    print(f"[bm25]删除索引失败,collection={collection}:{exc}")
    




    