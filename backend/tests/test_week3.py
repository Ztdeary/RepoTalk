"""Week 3 对比实验:30 题纯向量 vs 混合检索命中率对比。

用法(backend/ 目录下):
    uv run python -m tests.test_week3

判定规则:回答不含「资料中未提及」即为命中(hit)。
"""
import json
import sys
from pathlib import Path

from app.core.config import settings
from app.rag.repo_loader import iter_code_file
from app.rag.code_splitter import split_code_file
from app.rag.hybrid_retriever import build_bm25
from app.rag.pipeline import answer, answer_hybrid
from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector

COLLECTION = "requests"
REPO_ROOT = Path(__file__).parent / ".cache" / "requests"


def build_resources():
    """建一次 BM25 索引和向量库连接，全程复用。"""
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        timeout=30, max_retries=1,
    )
    store = PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION,
        connection=settings.database_url,
    )
    chunks = []
    for f in iter_code_file(REPO_ROOT):
        try:
            chunks.extend(split_code_file(f, "requests", REPO_ROOT))
        except Exception:
            pass
    bm25 = build_bm25(chunks)
    return store, bm25


def is_hit(answer_text: str) -> bool:
    return "资料中未提及" not in answer_text


def run_experiment():
    questions = json.loads(
        (Path(__file__).parent / "questions.json").read_text(encoding="utf-8")
    )
    store, bm25 = build_resources()

    results = []
    for q in questions:
        r_vec = answer(q["question"], store)
        r_hyb = answer_hybrid(q["question"], store, bm25)

        vec_hit = is_hit(r_vec["answer"])
        hyb_hit = is_hit(r_hyb["answer"])
        results.append({
            "id": q["id"], "type": q["type"],
            "vec_hit": vec_hit, "hyb_hit": hyb_hit,
            "vec_answer": r_vec["answer"][:60],
            "hyb_answer": r_hyb["answer"][:60],
        })
        mark = lambda h: "HIT" if h else "MISS"
        print(f"Q{q['id']:>2} [{q['type']:>15}]  vec={mark(vec_hit)}  hyb={mark(hyb_hit)}")

    # 汇总
    vec_total = sum(1 for r in results if r["vec_hit"])
    hyb_total = sum(1 for r in results if r["hyb_hit"])
    total = len(results)
    print(f"\n{'='*60}")
    print(f"纯向量命中率: {vec_total}/{total} = {vec_total/total*100:.0f}%")
    print(f"混合检索命中率: {hyb_total}/{total} = {hyb_total/total*100:.0f}%")
    print(f"提升: {(hyb_total-vec_total)/total*100:+.0f}%")

    # 按题型分组统计
    for qtype in ("func_location", "class_location", "usage"):
        group = [r for r in results if r["type"] == qtype]
        v = sum(1 for r in group if r["vec_hit"])
        h = sum(1 for r in group if r["hyb_hit"])
        n = len(group)
        print(f"  {qtype:>15}: vec={v}/{n}({v/n*100:.0f}%)  hyb={h}/{n}({h/n*100:.0f}%)")

    # 每题的回答存下来方便复盘
    out = Path(__file__).parent / "week3_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存到 {out}")

if __name__ == "__main__":
    run_experiment()
