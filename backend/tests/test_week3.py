"""检索消融实验:vector / hybrid / hybrid+rerank 三组对比。

零 LLM 成本:只有 embedding(每题一次)与 rerank(仅 full 组)调用。
判定口径:文件级二值相关性,以评测集 ground_truth_contexts 为准。

用法(backend/ 目录下):
    .venv/Scripts/python.exe -m tests.test_week3
    .venv/Scripts/python.exe -m tests.test_week3 --limit 3
    .venv/Scripts/python.exe -m tests.test_week3 --modes vector,hybrid

说明:
    --dataset 默认用 ragas_dataset.json,因为它带 ground_truth_contexts;
    questions.json 只有问题、没有标注,无法算检索指标。
"""

import argparse
import json
import time
from pathlib import Path

from langchain_openai import OpenAIEmbeddings
from langchain_postgres import PGVector

from app.core.config import settings
from app.rag.code_splitter import split_code_file
from app.rag.hybrid_retriever import build_bm25, hybrid_search
from app.rag.repo_loader import iter_code_file
from tests.eval.metrics import evaluate_queries

HERE = Path(__file__).parent
DEFAULT_DATASET = HERE / "ragas_dataset.json"
METRIC_KEYS = ("recall", "hit", "mrr", "ndcg")
METRIC_LABELS = {"recall": "Recall", "hit": "Hit", "mrr": "MRR", "ndcg": "nDCG"}


def build_resources(collection: str, repo_root: Path):
    """建一次向量库连接与 BM25 索引,全流程复用。"""
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        timeout=30,
        max_retries=1,
    )
    store = PGVector(
        embeddings=embeddings,
        collection_name=collection,
        connection=settings.database_url,
    )
    chunks = []
    for f in iter_code_file(repo_root):
        try:
            chunks.extend(split_code_file(f, collection, repo_root))
        except Exception:
            pass
    if not chunks:
        raise SystemExit(f"源码目录里没切出任何代码块:{repo_root}")
    return store, build_bm25(chunks), len(chunks)


def run_mode(mode: str, dataset: list[dict], store, bm25, k: int) -> tuple[dict, float]:
    """跑一种检索模式,返回聚合指标与耗时(秒)。"""
    per_query = []
    started = time.perf_counter()
    for item in dataset:
        error = None
        try:
            docs = hybrid_search(item["question"], store, bm25, mode=mode)
        except Exception as exc:
            docs = []
            error = f"{type(exc).__name__}: {exc}"
        per_query.append({
            "id": item.get("id"),
            "type": item.get("type"),
            "retrieved": [d.metadata.get("file_path", "") for d in docs],
            "relevant": item.get("ground_truth_contexts", []),
        })
        if error:
            print(f"  Q{item.get('id', '?')} [{mode}] ERR={error}")

    summary = evaluate_queries(per_query, k=k)
    for row in summary["per_query"]:
        rank = row["first_rank"]
        flag = f"rank={rank}" if rank else "MISS"
        print(f"  Q{row['id']:>2} [{mode:>6}] {flag}")
    return summary, time.perf_counter() - started


def render_markdown(results: dict, k: int, meta: dict) -> str:
    """把各组结果渲染成 Markdown 表格。"""
    lines = [
        f"# 检索消融结果(collection={meta['collection']}, k={k}, "
        f"数据集={meta['dataset']}, 题数={meta['n_questions']})",
        "",
        "| 模式 | " + " | ".join(f"{METRIC_LABELS[m]}@{k}" for m in METRIC_KEYS) + " | 耗时(s) |",
        "|---|" + "---|" * (len(METRIC_KEYS) + 1),
    ]
    for mode, summary in results.items():
        cells = " | ".join(f"{summary['overall'][m]:.4f}" for m in METRIC_KEYS)
        lines.append(f"| {mode} | {cells} | {summary['elapsed_sec']:.1f} |")

    lines += ["", "## 分题型", "", "| 模式 | 题型 | 题数 | " + " | ".join(
        f"{METRIC_LABELS[m]}@{k}" for m in METRIC_KEYS) + " |",
        "|---|---|---|" + "---|" * len(METRIC_KEYS)]
    for mode, summary in results.items():
        for qtype, metric in summary["by_type"].items():
            n = sum(1 for q in summary["per_query"] if q["type"] == qtype)
            cells = " | ".join(f"{metric[m]:.4f}" for m in METRIC_KEYS)
            lines.append(f"| {mode} | {qtype} | {n} | {cells} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="检索消融实验(vector/hybrid/full)")
    parser.add_argument("--collection", default="requests")
    parser.add_argument("--repo-root", default=None, help="默认 tests/.cache/<collection>")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--k", type=int, default=6, help="计算指标时截断的条数")
    parser.add_argument("--modes", default="vector,hybrid,full")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题(冒烟用)")
    parser.add_argument("--out", default=str(HERE / "week3_ablation.json"))
    parser.add_argument("--md-out", default=str(HERE / "week3_ablation.md"))
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        raise SystemExit(f"评测集不存在:{dataset_path}")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset = [x for x in dataset if x.get("ground_truth_contexts")]
    if args.limit > 0:
        dataset = dataset[: args.limit]
    if not dataset:
        raise SystemExit("评测集为空或没有 ground_truth_contexts")

    repo_root = Path(args.repo_root) if args.repo_root else HERE / ".cache" / args.collection
    if not repo_root.is_dir():
        raise SystemExit(f"源码目录不存在:{repo_root}(先跑 Week 3 的克隆步骤)")

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = set(modes) - {"vector", "hybrid", "full"}
    if unknown:
        raise SystemExit(f"未知模式:{unknown}")

    print(f"[1/3] 构建资源 collection={args.collection} repo={repo_root}")
    store, bm25, n_chunks = build_resources(args.collection, repo_root)
    print(f"      BM25 索引块数={n_chunks}")

    probe = store.similarity_search("def request", k=1)
    if not probe:
        raise SystemExit(f"collection={args.collection} 里没有数据,先 ingest 再跑")
    print(f"      向量库探针 file_path={probe[0].metadata.get('file_path')}")

    print(f"[2/3] 跑 {len(modes)} 组模式,共 {len(dataset)} 题")
    results = {}
    for mode in modes:
        summary, elapsed = run_mode(mode, dataset, store, bm25, args.k)
        summary["elapsed_sec"] = elapsed
        results[mode] = summary
        print(f"      {mode}: " + " ".join(
            f"{METRIC_LABELS[m]}={summary['overall'][m]:.4f}" for m in METRIC_KEYS
        ) + f"  ({elapsed:.1f}s)")

    try:
        dataset_label = dataset_path.relative_to(HERE).as_posix()
    except ValueError:
        dataset_label = dataset_path.name
    meta = {
        "collection": args.collection,
        "k": args.k,
        "dataset": dataset_label,
        "n_questions": len(dataset),
        "n_bm25_chunks": n_chunks,
    }
    report = {"meta": meta, "results": results}

    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(results, args.k, meta)
    Path(args.md_out).write_text(markdown, encoding="utf-8")

    print("[3/3] 汇总")
    print(markdown)
    print(f"明细: {out}")
    print(f"表格: {args.md_out}")


if __name__ == "__main__":
    main()
