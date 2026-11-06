from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from app.core.config import settings
from app.core.byok import LLMCred
# LLMCred:访客自带Key校验通过后的凭证对象(base_url/api_key/model)。
# 只做类型标注用,不在这里做任何校验——校验已经在路由层做完了
from app.rag.hybrid_retriever import hybrid_search as hybrid_retriever
from collections.abc import AsyncIterator
# AsyncIterator:异步迭代器的类型标注,表示"这个函数能配合async for逐段产出数据" 
# 必须从collections.abc导入,Python 3.12+禁止从collections导入抽象基类

def _make_llm(
    llm_cred:LLMCred|None=None,
    max_tokens:int|None=None,
)->ChatOpenAI:
    """统一构造 LLM 客户端:有访客凭证就用访客的,没有就用站主的。

    为什么必须集中到一处:
    原来 answer / stream_answer_hybrid / generate_answer 各 new 了一个 ChatOpenAI,
    加 BYOK 功能时如果三处各改一遍,漏掉一处就会出现
    "流式回答偷偷用了站主的Key"这种很难发现的账单价差。
    现在三个函数都调本函数,改一处三处生效。

    参数含义:
    llm_cred  - 访客自带凭证;None = 走站主配置(只读演示/管理员)
    max_tokens- 输出 token 上限;None = 不传该参数,由模型按默认生成
                (演示模式传 320,把单次成本压到几分钱以内)
    """
    # 关键坑点:langchain-openai 0.3.x 把构造参数 max_tokens 的别名改成了
    # max_completion_tokens(OpenAI 官方新字段名),实际发出去的请求体里是
    # "max_completion_tokens": 320。DeepSeek 兼容接口只认老名字 "max_tokens",
    # 于是这个上限被静默忽略——实测 320 的限制完全没生效,一次回答能写 3000+ 字。
    # 第一次尝试用 model_kwargs 塞 max_tokens,被 langchain 归一化成了
    # max_completion_tokens,仍然无效(本地打印请求体确认过)。
    # 最终方案:走 extra_body —— 这是 OpenAI SDK 的"逃生口",
    # 里面的字段不经过任何别名转换,原样合并进请求体,DeepSeek 就能识别。
    # max_tokens=None 时给 None/空字典,表示"不传这个字段,用服务商默认值"。
    extra_body = {"max_tokens": max_tokens} if max_tokens is not None else {}

    if llm_cred is not None:
        # 访客的Key:model/base_url 都来自访客请求头,费用记在访客账上
        return ChatOpenAI(
            model=llm_cred.model,
            base_url=llm_cred.base_url,
            api_key=llm_cred.api_key,
            temperature=0, # 问答要稳定,不要创意发挥
            timeout=60,
            max_retries=1, # 只重试1次:重试会真实产生额外费用,不能放任
            extra_body=extra_body or None,
            # extra_body or None:空字典时传 None,避免给请求体塞一个没有意义的空对象
        )
    # 站主的Key:只读演示、管理员问答走这里
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
        timeout=60,
        max_retries=1,
        extra_body=extra_body or None,
    )

# 构造加载＋切片函数
def split_text(text:str)->list[str]:
    """将文本切片为多个段落,
    每个段落的长度(chunk_size)为800 字个字符，
    重叠(chunk_overlap)为 100 字个字符。"""
    splitter=RecursiveCharacterTextSplitter(chunk_size=800,chunk_overlap=100)
    return splitter.split_text(text)

# 构造向量化＋存库函数
def build_index(text: str, collection: str = "demo"):
    """向量化+存库:这里是对最小闭环的一次性Demo。"""
    chunks = split_text(text)
    # PGVector 负责把 vector 存进 Postgres（依赖 pgvector 扩展，见 docker-compose）
    store = PGVector(
        embeddings=OpenAIEmbeddings(
            model=settings.embedding_model,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            timeout=30, max_retries=1
        ),
        collection_name=collection,
        connection=settings.database_url,
        pre_delete_collection= True,# 建索引前先清掉同名 collection，保证"重新灌 = 覆盖"
    )
    store.add_texts(chunks)
    return store

# 构造检索+生成函数
def answer(question: str, store: PGVector)->dict:
    """检索+生成:RAG的核心两步。
    retriever从向量库捞top-k相关块 → 拼进prompt → 让LLM基于这些块回答。"""
    retriever = store.as_retriever(search_kwargs={"k":4})
    prompt=ChatPromptTemplate.from_template(
        # ChatPromptTemplate:langchain的模板类,{}里的变量运行时会替换成真实内容
        # from_template:从一段字符串快速构造模板,不需要手动声明input_variables
        """你是源码问答助手。严格依据下面的上下文回答,不要编造上下文里没有的代码。
        上下文每块开头都标了 文件路径、行号、符号名;回答涉及位置时直接引用这些标注。
        如果上下文里有相关代码,请给出文件路径、行号和解释;信息不完整时先说"已检索到的相关代码是...",再基于可见代码作答。
        只有当上下文完全没有相关代码时,才回答"资料中未提及"。
        上下文: {context}
        问题: {question}
        """
        # 原文只写了"若上下文没有答案,直接说 资料中未提及",过于绝对:
        # 模型只要觉得上下文不完整就直接摆烂,哪怕里面明明有相关文件
        # 新版本把"有相关代码但不完整"和"完全没有相关代码"区分开,
        # 前者要求给出文件和行号,后者才允许说"资料中未提及"
        # {context}:运行时会替换成拼接好的代码块(带文件/行号/符号头)
        # {question}:运行时会替换成用户原始问题
    )
    # 走统一的工厂函数:这是老的演示函数,固定用站主的Key
    llm = _make_llm()
    docs = retriever.invoke(question)
    context="\n\n".join(d.page_content for d in docs)
    chain=prompt| llm| StrOutputParser()
    return {
        "answer": chain.invoke({"context":context,"question":question}),
        "sources": [{"content": d.page_content[:200]} for d in docs],
    }

# 构造流式生成函数
async def stream_answer_hybrid(
    question:str,
    docs:list,
    llm_cred:LLMCred|None=None,
    max_tokens:int|None=None,
)->AsyncIterator[str]:
    """流式版生成:输入检索好的docs,逐token产出回答文本。
    与answer_hybrid的关系:
    prompt模板、元数据头拼接方式、LLM参数三者完全一致——只变"取结果的方式",不变"智能"。
    answer_hybrid用chain.invoke等LLM写完整篇再一次返回;
    本函数用chain.astream拿一个异步迭代器,LLM每生成一小段就yield一段。
    为什么检索不在这个函数里做?
    hybrid_search是同步阻塞代码(BM25打分+rerank的HTTP调用都是同步),
    必须丢进线程池执行,而"丢线程池+等待期发心跳"是HTTP路由层的职责。
    本函数保持纯净:不关心SSE协议,不关心线程池,只负责"给定资料,流式写回答"。""" 
    # 1.拼上下文:和answer_hybrid一字不差 
    # 每块前面加元数据头(文件路径+行号+符号名), 
    # 没有它LLM无法回答"在哪个文件第几行"这类位置问题
    context="\n\n".join(
        f"# 文件:{d.metadata.get('file_path')}第{d.metadata.get('start_line')}--{d.metadata.get('end_line')}行符号:{d.metadata.get('symbol')}\n{d.page_content}"
        for d in docs
    )
    # 2.prompt模板:和answer_hybrid完全相同 
    # "仅根据上下文回答"是抑制幻觉的第一道防线
    prompt=ChatPromptTemplate.from_template(
        # ChatPromptTemplate:langchain的模板类,{}里的变量运行时会替换成真实内容
        # from_template:从一段字符串快速构造模板,不需要手动声明input_variables
        """你是源码问答助手。严格依据下面的上下文回答,不要编造上下文里没有的代码。
        上下文每块开头都标了 文件路径、行号、符号名;回答涉及位置时直接引用这些标注。
        如果上下文里有相关代码,请给出文件路径、行号和解释;信息不完整时先说"已检索到的相关代码是...",再基于可见代码作答。
        只有当上下文完全没有相关代码时,才回答"资料中未提及"。
        上下文: {context}
        问题: {question}
        """
        # 原文只写了"若上下文没有答案,直接说 资料中未提及",过于绝对:
        # 模型只要觉得上下文不完整就直接摆烂,哪怕里面明明有相关文件
        # 新版本把"有相关代码但不完整"和"完全没有相关代码"区分开,
        # 前者要求给出文件和行号,后者才允许说"资料中未提及"
        # {context}:运行时会替换成拼接好的代码块(带文件/行号/符号头)
        # {question}:运行时会替换成用户原始问题
    )
    # 3.LLM参数:和answer_hybrid完全相同 
    # temperature=0问答要稳定;timeout=60在流式里含义是"相邻两个token间隔超60秒才算超时", 
    # 正常生成不可能60秒憋不出一个词,安全
    llm=_make_llm(llm_cred,max_tokens)
    # 访客带了Key -> 用访客的模型/Key;否则用站主配置。
    # max_tokens 只有只读演示才非空,用来把单次回答的成本封顶
    # 4.组装链:prompt填变量->送LLM->StrOutputParser把输出剥成纯字符串
    chain=prompt|llm|StrOutputParser()
    # 5.异步流式调用: 
    # chain.astream返回异步迭代器,必须用async for消费 
    # LLM每生成一个小片段(通常几个字)循环体就执行一次 
    # 因为链尾挂了StrOutputParser,吐出来的直接是str,不用再.content 取值
    async for token in chain.astream({"context":context,"question":question}):
        if token:
            # 流式协议偶尔会吐空帧(比如第一帧只带角色标记不带内容)
            # 空字符串推给前端没意义,直接跳过
            yield token
            # yield:函数暂停在这,把这一段token交出去
            # 调用方(routes层的SSE生成器)拿走后,下次循环从这里继续

def generate_answer(
    question:str,
    docs:list,
    llm_cred:LLMCred|None=None,
    max_tokens:int|None=None,
)->dict:
    """给定检索好的docs,生成回答。
    从answer_hybrid里原样抽出:prompt模板、元数据头拼接、LLM参数全部一致
    只把"检索"和"生成"两步解开,让缓存命中时能跳过检索直接进生成。"""
    prompt=ChatPromptTemplate.from_template(
        # ChatPromptTemplate:langchain的模板类,{}里的变量运行时会替换成真实内容
        # from_template:从一段字符串快速构造模板,不需要手动声明input_variables
        """你是源码问答助手。严格依据下面的上下文回答,不要编造上下文里没有的代码。
        上下文每块开头都标了 文件路径、行号、符号名;回答涉及位置时直接引用这些标注。
        如果上下文里有相关代码,请给出文件路径、行号和解释;信息不完整时先说"已检索到的相关代码是...",再基于可见代码作答。
        只有当上下文完全没有相关代码时,才回答"资料中未提及"。
        上下文: {context}
        问题: {question}
        """
        # 原文只写了"若上下文没有答案,直接说 资料中未提及",过于绝对:
        # 模型只要觉得上下文不完整就直接摆烂,哪怕里面明明有相关文件
        # 新版本把"有相关代码但不完整"和"完全没有相关代码"区分开,
        # 前者要求给出文件和行号,后者才允许说"资料中未提及"
        # {context}:运行时会替换成拼接好的代码块(带文件/行号/符号头)
        # {question}:运行时会替换成用户原始问题
    )
    llm=_make_llm(llm_cred,max_tokens)
    # 与 stream_answer_hybrid 共用同一个工厂:两条输出路径的"Key选择 + 限长"规则
    # 永远一致,不会出现"流式省着花钱、非流式却敞开花"的漏洞
    context="\n\n".join(  # 每块前面加元数据头,LLM才能回答"在哪个文件"这类位置问题
        f"# 文件:{d.metadata.get('file_path')}第{d.metadata.get('start_line')}--{d.metadata.get('end_line')}行符号:{d.metadata.get('symbol')}\n{d.page_content}"
        for d in docs
    )
    chain=prompt|llm|StrOutputParser() # prompt填变量->送LLM->把输出剥成纯字符串
    return{
        "answer":chain.invoke({"context":context,"question":question}),
        "source":[{ # 注意保持"source"这个key名不变,和旧响应结构一致,前端/验收脚本不受影响
            "file_path":d.metadata.get("file_path"), # 相对路径,前端跳转用
            "start_line":d.metadata.get("start_line"), # 起始行,前端定位用
            "end_line":d.metadata.get("end_line"), # 结束行,前端定位用
            "symbol":d.metadata.get("symbol"), # 函数/类名,前端展示用
            "content":d.page_content[:200], # 前200字摘要,预览用
        }
        for d in docs
    ],
}

# 历史会话标题:交给LLM总结,要求短、只输出标题本身
async def summarize_title(question:str,answer:str)->str:
    """用一轮问答生成会话标题(不超过14个字)。

    为什么放在 pipeline 里:标题也走 LLM,复用 _make_llm 的 Key 选择逻辑,
    管理员/访客用的是同一套站主 Key 配置,不会出现"回答能用、标题却没Key"。
    失败由调用方兜底(截取问题前几个字),这里不吞异常,方便日志排查。
    截断输入:标题只需要主题信息,问题和回答各送前几百字就够,
    既省 token 也避免超长回答把标题带偏。
    """
    prompt=ChatPromptTemplate.from_template(
        """请用不超过14个汉字概括下面这轮代码问答的主题。
只输出标题本身:不要引号、不要书名号、不要标点结尾、不要解释、不要分行。
问题:{question}
回答:{answer}
"""
    )
    llm=_make_llm(max_tokens=48)
    # 48 个 token 足够 14 个汉字;extra_body 方式已在 _make_llm 里处理好
    chain=prompt|llm|StrOutputParser()
    title=await chain.ainvoke({
        "question":question[:400],
        "answer":answer[:800],
    })
    # 清洗:模型偶尔会带引号/书名号/换行,统一削掉,再限制长度
    title=" ".join(str(title).split())
    title=title.strip(" \"'“”‘’《》【】#*-:：。.,，")
    return title[:24]

# 构造混合检索+生成函数
def answer_hybrid(question:str,store:PGVector,bm25)->dict:
    """混合检索+生成(薄封装版)。
    行为与旧版完全一致,内部改为调generate_answer,消除重复代码。
    Week 3对比实验脚本调的就是它,签名和返回结构都不能动。"""
    docs=hybrid_retriever(question,store,bm25) # 检索:双路初检+RRF融合+重排,top-3
    return generate_answer(question,docs)
