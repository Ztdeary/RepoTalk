"""Week 6—RAGAS四指标评测脚本。
评测指标:
  1. context_precision  — 上下文精度:检索到的块里有多少是真正相关的
  2. context_recall     — 上下文召回:标准答案需要的信息有多少被检索到了
  3. faithfulness       — 忠实度:回答里的每句话是否都能在检索上下文中找到依据
  4. answer_relevancy   — 回答相关性:回答是否切题、有没有答非所问
用法(在 backend/ 目录下):
    uv run python -m tests.test_week6
前置条件:
  1. docker compose up -d(pgvector+redis必须跑着)
  2. requests仓库已ingest进collection="requests"(跑过Week 3的实验就有)
  3. .env里LLM_API_KEY/EMBEDDING_API_KEY有效
"""

# ============================================================
# 第1部分:标准库与第三方库导入
# ============================================================
import json # json:把评测结果序列化成JSON存盘
import sys # sys:取命令行参数、控制退出码

from pathlib import Path  # Path:跨平台路径处理,比os.path更安全
from typing import Any  # Any:类型标注,表示"任意类型"
# ----RAGAS评测核心库 ----
# ragas是RAG评测的事实标准库,内部用LLM当裁判给四指标打分
from ragas import evaluate     # evaluate:RAGAS的主入口函数,跑评测并返回结果
from ragas.metrics import (    # 从 ragas.metrics子模块导入四个指标类
    context_precision,         # 上下文精度指标(需要LLM判检索块是否相关)
    context_recall,            # 上下文召回指标(需要ground_truth+LLM)
    faithfulness,              # 忠实度指标(需要LLM拆回答为断言并核对上下文)
    answer_relevancy,          # 回答相关性指标(需要LLM+Embedding)
)

# ----HuggingFace datasets库 ----
# RAGAS的evaluate()要求输入是datasets.Dataset格式(不是普通list)
# Dataset是HuggingFace的表格数据结构,每列是一个字段
from datasets import Dataset

# ----LangChain相关 ----
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
# ChatOpenAI:通用的OpenAI协议LLM封装,DeepSeek兼容这个协议
# OpenAIEmbeddings:通用的OpenAI协议Embedding封装,硅基流动兼容

from langchain_postgres import PGVector
# PGVector:LangChain对pgvector的封装,负责向量存取

# ---- 项目内部模块 ----
from app.core.config import settings                    # settings:全局配置单例,从.env读
from app.rag.repo_loader import iter_code_file          # iter_code_file:遍历仓库代码文件
from app.rag.code_splitter import split_code_file       # split_code_file:AST切分代码为块
from app.rag.hybrid_retriever import build_bm25         # build_bm25:用块列表建BM25索引
from app.rag.hybrid_retriever import hybrid_search      # hybrid_search:混合检索(向量+BM25+RRF+rerank)
from app.rag.pipeline import generate_answer            # generate_answer:给定docs生成回答

# ============================================================
# 第2部分:常量定义
# ============================================================
COLLECTION = "requests"
# COLLECTION:要评测的pgvector collection名
# 与Week 3实验保持一致,评测的是requests仓库的RAG效果
REPO_ROOT = Path(__file__).parent / ".cache" / "requests"
# REPO_ROOT:requests仓库源码的本地路径
# Path(__file__):当前脚本文件的绝对路径(.../tests/test_week6.py)
# .parent:取它的父目录(.../tests/)
# /".cache"/"requests":拼接子目录,得到.../tests/.cache/requests
# 这是Week 3实验时克隆下来的requests源码

# ============================================================
# 第3部分:构建RAG资源(向量库连接+BM25 索引)
# ============================================================
def build_resources() -> tuple[PGVector, Any]:
    """构建RAG评测所需的两个资源:向量库连接+BM25索引。
    返回:
        tuple[PGVector, BM25Retriever]:
            第一个元素是PGVector连接(用于向量检索)
            第二个元素是BM25Retriever对象(用于关键词检索)
    为什么要单独建BM25?
        线上问答时BM25是worker解析仓库时建好存进Redis的,
        评测脚本不走worker,得自己从源码重建一份。
    """
    # ----3.1构造Embedding客户端----
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        timeout=30,
        max_retries=1,
    )
    # ----3.2构造PGVector向量库连接----
    store = PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION,
        connection=settings.database_url,
    )
    # 注意:这里没传pre_delete_collection=True,只读不写
    # 评测脚本不能删数据,否则会把Week 3灌好的向量库清空

    # ----3.3遍历源码+AST切分,重建BM25索引----
    chunks:list[dict]=[]
    # chunks:装载所有切分后的代码块,每个块是{content,metadata}字典
    for f in iter_code_file(REPO_ROOT):
        try:
            chunks.extend(split_code_file(f,"requests",REPO_ROOT))
        except Exception:
            # 单个文件切分失败不影响整体,跳过继续
            pass
    bm25=build_bm25(chunks)
    # build_bm25:用块列表在内存里建BM25关键词索引
    # 内部把symbol拼进索引文本,让BM25能匹配函数名/类名
    return store,bm25
    # 返回两个资源,评测循环里反复用

# ============================================================
# 第4部分:跑RAG流水线,收集每条样本的answer+contexts
# ============================================================
def run_rag_for_dataset(dataset:list[dict])->list[dict]:
    """对评测集里的每个问题跑一遍RAG流水线,收集回答和检索上下文。
    参数:
        dataset:评测数据集,每条是{question,ground_truth,ground_truth_contexts}
    返回:
        list[dict]:每条新增answer和contexts字段
        新增字段:
        answer:str — RAG系统生成的回答
        contexts:list[str] — 检索到的上下文块内容列表
    """
    store,bm25=build_resources()
    # 建一次资源,全评测复用,避免每个问题都重建连接
    result:list[dict]=[]
    for item in dataset:
        question=item["question"]
        # question:用户问题,从样本字典里取出来

        # ----4.1混合检索----
        docs=hybrid_search(question,store,bm25)

        # ----4.2生成回答----
        answer_dict=generate_answer(question,docs)

        # ----4.3收集结果----
        result.append({
            "question":question,
            "answer":answer_dict["answer"],
            "contexts":[d.page_content for d in docs],
            "ground_truth":item["ground_truth"], # 标准答案(给context_recall用)
            "ground_truth_contexts":item["ground_truth_contexts"] # 标准答案依据的文件路径
        })
        # 这五个字段就是RAGAS评测需要的全部输入

        # ----4.4打印进度----
        mark="HIT" if "资料中未提及" not in answer_dict["answer"] else "MISS"
        # mark:简单标记这次回答有没有命中(不含"资料中未提及"就算命中)
        print(f"[RAG] Q{item.get('id','?'):>2}{mark} contexts={len(docs)}")
        # '?':如果样本没有id,用?占位
        # >2:右对齐,占2个字符宽度
    return result

# ============================================================
# 第5部分:构造RAGAS评测的LLM和Embedding客户端
# ============================================================

class ForceN1LLM(ChatOpenAI):
    """ChatOpenAI子类:强制所有生成调用使用n=1。
    原因:DeepSeek等兼容API不支持n>1(一次只返回1个结果),
    但ragas内部的answer_relevancy等指标会请求n=3生成多个问题,
    导致API报错"Invalid n value"。
    通过重写_generate方法(实际发起API请求的地方),把n强制设为1。
    继承ChatOpenAI而不是包装,是为了保留LangChain的完整接口,
    让ragas能正确识别和包装这个LLM。
    """
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        # _generate:LangChain 内部实际发起 API 请求的同步方法
        kwargs.pop("n",None)  # 删掉调用方传的 n 参数
        if "model_kwargs" in kwargs:
            kwargs["model_kwargs"].pop("n",None)  # 也删掉 model_kwargs 里的 n
        return super()._generate(messages,stop=stop,run_manager=run_manager,n=1,**kwargs)

    async def _agenerate(self,messages,stop=None,run_manager=None,**kwargs):
        # _agenerate:LangChain 内部实际发起 API 请求的异步方法
        # ragas 评测时可能用异步模式,所以也要覆盖
        kwargs.pop("n",None)  # 删掉调用方传的 n 参数
        if "model_kwargs" in kwargs:
            kwargs["model_kwargs"].pop("n",None)  # 也删掉 model_kwargs 里的 n
        return await super()._agenerate(messages,stop=stop,run_manager=run_manager,n=1,**kwargs)


def get_judge_llm()->ChatOpenAI:
    """构造给RAGAS当裁判用的LLM。
    RAGAS的四指标里有三个需要LLM当裁判:
      -context_precision:LLM判每个检索块是否与问题相关
      -faithfulness:LLM把回答拆成断言,再判每个断言是否在上下文里
      -answer_relevancy:LLM判回答是否切题
    用DeepSeek当裁判即可,不需要额外的评测专用模型。
    """
    return ForceN1LLM(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
        timeout=120,
        max_retries=2,
        n=1,
    )

def get_judge_embeddings()->OpenAIEmbeddings:
    """构造给RAGAS用的Embedding客户端。
    answer_relevancy指标需要算问题和回答的语义相似度,要用到Embedding。
    """
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        timeout=30,
        max_retries=1,
    )

# ============================================================
# 第6部分:主函数——跑评测并输出报告
# ============================================================
def main():
    """RAGAS评测主流程。
    执行步骤:
      1. 加载评测数据集
      2. 跑RAG流水线收集answer+contexts
      3. 转成HuggingFace Dataset格式
      4. 调ragas.evaluate()计算四指标
      5. 打印汇总表+存JSON报告
    """

    # ----6.1加载评测数据集----
    dataset_path=Path(__file__).parent/"ragas_dataset.json"
    # dataset_path:评测数据集文件的路径
    # Path(__file__).parent是tests/目录,ragas_dataset.json就在同级
    dataset=json.loads(dataset_path.read_text(encoding="utf-8"))
    # dataset_path.read_text(encoding="utf-8"):读取文件内容为字符串
    # json.loads(...):把JSON字符串解析成Python list[dict]
    print(f"[step 1]加载评测集:{len(dataset)}条问题")

    # ----6.2 跑RAG流水线----
    print("[step 2]跑RAG流水线,收集answer+contexts...")
    rag_result=run_rag_for_dataset(dataset)
    # rag_result:每条样本现在都有question/answer/contexts/ground_truth

    # ----6.3转成HuggingFace Dataset格式----
    # RAGAS的evaluate()只接受datasets.Dataset,不接受普通list
    # 所以要把rag_result转成Dataset格式
    rag_dataset=Dataset.from_list(rag_result)
    # Dataset.from_list():把list[dict]转成Dataset 对象
    # 每个dict的key变成Dataset的列名,value变成列数据

    # ----6.4构造裁判LLM和Embedding----
    judge_llm=get_judge_llm()
    # judge_llm:给RAGAS当裁判的LLM
    judge_embeddings=get_judge_embeddings()
    # judge_embeddings:给answer_relevancy算相似度用的Embedding
    
    # ----6.5运行RAGAS评测----
    print("[step 3]运行RAGS四指标评测(LLM裁判,可能较慢)...")
    result=evaluate(
        # evaluate:RAGAS的主函数,返回EvaluationResult对象
        dataset=rag_dataset,  # dataset:要评测的数据集
        metrics=[  # metrics:要计算的指标列表
            context_precision, # 上下文精度
            context_recall, # 上下文召回
            faithfulness, # 忠实度
            answer_relevancy, # 回答相关性
        ],
        llm=judge_llm, # llm:当裁判的LLM(给需要LLM的指标用)
        embeddings=judge_embeddings,  # embeddings:当裁判的Embedding(给answer_relevancy用)
        raise_exceptions=False,   # raise_exceptions=False:单条样本失败不中断整个评测
    )
    
    # ----6.6打印汇总表----
    print(f"\n{'='*60}") # '='*60:打印60个=号,分隔不同评测结果
    print(f"RAGAS评测结果(collection={COLLECTION})")
    print(f"\n{'='*60}")

    # result是EvaluationResult对象,支持dict式访问
    # 每个指标的得分是0~1的浮点数,乘100转成百分比更直观
    # 注意:如果部分样本评测失败,ragas可能返回列表,需要取均值
    import numpy as np
    def _to_float(val):
        """把ragas返回的得分转成float。如果是列表/Series就取均值(跳过NaN)。"""
        if isinstance(val,(list,np.ndarray)):
            arr=np.array(val,dtype=float)
            return float(np.nanmean(arr)) if np.any(~np.isnan(arr)) else 0.0
        try:
            return float(val)
        except (TypeError,ValueError):
            return 0.0

    cp=_to_float(result["context_precision"]) # context_precision:上下文精度得分
    cr=_to_float(result["context_recall"])  # context_recall:上下文召回得分
    fa=_to_float(result["faithfulness"]) # faithfulness:忠实度得分
    ar=_to_float(result["answer_relevancy"])  # answer_relevancy:回答相关性得分

    print(f"context_precision:{cp:.4f}({cp*100:.1f})%")
    print(f"context_recall:{cr:.4f}({cr*100:.1f})%")
    print(f"faithfulness:{fa:.4f}({fa*100:.1f})%")
    print(f"answer_relevancy:{ar:.4f}({ar*100:.1f})%")
    # :.4f:保留4位小数
    # :.1f%:乘100后保留1位小数加百分号
    print(f"{'='*60}")

    # ----6.7存JSON报告----
    # result可以直接转dict,存下来方便复盘和做简历数字
    report={
        "collection":COLLECTION,  # collection:评测的仓库
        "sample_count":len(dataset),   # sample_count:样本数
        "metrics":{ # metrics:四指标得分(原始0~1浮点数)
            "context_precision":float(cp),
            "context_recall":float(cr),
            "faithfulness":float(fa),
            "answer_relevancy":float(ar),
        },
        "samples":rag_result,   # samples:每条样本的完整数据(含answer/contexts)
    }
    # float():把numpy浮点数转成Python原生float,json才能序列化

    report_path=Path(__file__).parent/"week6_ragas_report.json"
    # report_path:报告输出路径,tests/week6_ragas_report.json
    report_path.write_text(
        json.dumps(report,ensure_ascii=False,indent=2),
        # ensure_ascii=False:中文不转义,文件里直接是中文
        # indent=2:缩进2空格,方便人读
        encoding="utf-8"
    )
    print(f"\n详细报告已保存到{report_path}")

    # ----6.8退出码----
    # 如果四指标都>=0.6(及格线),退出码0;否则1
    # 这样可以接进CI/CD流水线,指标不达标就阻塞部署
    all_pass=all(m>=0.6 for m in(cp,cr,fa,ar))
    # all():可迭代对象里所有元素都为真才返回True
    # m>=0.6:每个指标是否>=0.6
    if all_pass:
        print("[PASS]四指标>=0.6,评测通过")
        sys.exit(0)
    else:
        print("[FAIL]存在指标<0.6,请排查")
        sys.exit(1)

# ============================================================
# 第7部分:脚本入口
# ============================================================

if __name__=="__main__":
    # __name__ == "__main__":只有直接运行本脚本时才为真
    # 被import时__name__是模块名("tests.test_week6"),不会执行main()
    main()
