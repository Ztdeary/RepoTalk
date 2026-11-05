from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_postgres import PGVector
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from app.core.config import settings
from app.rag.hybrid_retriever import hybrid_search as hybrid_retriever
from collections.abc import AsyncIterator
# AsyncIterator:异步迭代器的类型标注,表示"这个函数能配合async for逐段产出数据" 
# 必须从collections.abc导入,Python 3.12+禁止从collections导入抽象基类

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
    llm = ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0, # 问答要稳定，别让它发挥创意
        timeout=60, max_retries=1    
    )
    docs = retriever.invoke(question)
    context="\n\n".join(d.page_content for d in docs)
    chain=prompt| llm| StrOutputParser()
    return {
        "answer": chain.invoke({"context":context,"question":question}),
        "sources": [{"content": d.page_content[:200]} for d in docs],
    }

# 构造流式生成函数
async def stream_answer_hybrid(question:str,docs:list)->AsyncIterator[str]:
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
    llm=ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
        timeout=60,max_retries=1
    )
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

def generate_answer(question:str,docs:list)->dict:
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
    llm=ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0, # 问答要稳定，别让它发挥创意
        timeout=60, max_retries=1    
    ) # LLM参数与answer_hybrid完全相同
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

# 构造混合检索+生成函数
def answer_hybrid(question:str,store:PGVector,bm25)->dict:
    """混合检索+生成(薄封装版)。
    行为与旧版完全一致,内部改为调generate_answer,消除重复代码。
    Week 3对比实验脚本调的就是它,签名和返回结构都不能动。"""
    docs=hybrid_retriever(question,store,bm25) # 检索:双路初检+RRF融合+重排,top-3
    return generate_answer(question,docs)
