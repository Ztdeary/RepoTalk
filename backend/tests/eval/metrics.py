"""检索质量指标库:纯函数、无第三方依赖，便于单测与复用。

口径约定:
- retrieved:按相关性从高到低排列的"文件路径"列表(检索结果的唯一身份)
- relevant: 标准答案涉及的"文件路径"集合,来自评测集 ground_truth_contexts

为什么用文件级而不是 chunk 级:
评测集只标到文件(src/requests/api.py),没有 chunk 级标注;
文件级判定更粗,但可复现、无人工主观性。为避免"同一文件的多个块"
把指标刷高,所有指标都会先按文件去重、只保留首次出现的位置。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# 指标短名 -> 报告列名,保持顺序稳定
METRIC_NAMES = ("recall", "hit", "mrr", "ndcg")

METRIC_LABELS = {
    "recall": "Recall",
    "hit": "Hit",
    "mrr": "MRR",
    "ndcg": "nDCG",
}


def normalize_path(path: str) -> str:
    """统一路径分隔符与前缀,让 Windows/Linux 与不同来源的路径可比。"""
    text = str(path).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def unique_paths(paths: Sequence[str]) -> list[str]:
    """按文件去重,保留首次出现的顺序(重复块只算一次)。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in paths:
        path = normalize_path(raw)
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def _relevant_set(relevant: Iterable[str]) -> set[str]:
    return {p for p in (normalize_path(x) for x in relevant) if p}


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """相关文件被召回的覆盖率:命中的相关文件数 / 全部相关文件数。"""
    if k <= 0:
        return 0.0
    rel = _relevant_set(relevant)
    if not rel:
        return 0.0
    top = set(unique_paths(retrieved)[:k])
    return len(top & rel) / len(rel)


def hit_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """top-k 内是否至少出现一个相关文件(0 或 1)。"""
    if k <= 0:
        return 0.0
    rel = _relevant_set(relevant)
    top = set(unique_paths(retrieved)[:k])
    return 1.0 if top & rel else 0.0


def first_relevant_rank(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> int | None:
    """第一个相关文件的名次(从 1 开始);top-k 内没有则返回 None。"""
    if k <= 0:
        return None
    rel = _relevant_set(relevant)
    for rank, path in enumerate(unique_paths(retrieved)[:k], start=1):
        if path in rel:
            return rank
    return None


def mrr_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """平均倒数排名:第一个相关文件名次的倒数,没有命中记 0。"""
    rank = first_relevant_rank(retrieved, relevant, k)
    return 1.0 / rank if rank else 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """按二值相关性计算 nDCG@k,对"相关文件出现得越早"给越高分。"""
    if k <= 0:
        return 0.0
    rel = _relevant_set(relevant)
    if not rel:
        return 0.0
    top = unique_paths(retrieved)[:k]
    dcg = sum(1.0 / math.log2(i + 2) for i, path in enumerate(top) if path in rel)
    ideal_hits = min(len(rel), len(top))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return dcg / idcg if idcg else 0.0


def metrics_for_query(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> dict:
    """单条 query 的四个指标。"""
    return {
        "recall": recall_at_k(retrieved, relevant, k),
        "hit": hit_at_k(retrieved, relevant, k),
        "mrr": mrr_at_k(retrieved, relevant, k),
        "ndcg": ndcg_at_k(retrieved, relevant, k),
    }


def _mean(rows: list[dict]) -> dict:
    if not rows:
        return {name: 0.0 for name in METRIC_NAMES}
    return {name: sum(row[name] for row in rows) / len(rows) for name in METRIC_NAMES}


def evaluate_queries(per_query: Sequence[dict], k: int) -> dict:
    """聚合评测结果。

    per_query 每条:{"id", "type", "retrieved": [...], "relevant": [...]}
    返回 {"n": 条数, "overall": {...}, "by_type": {type: {...}}, "per_query": [...]}
    """
    scored: list[dict] = []
    for item in per_query:
        row = metrics_for_query(item.get("retrieved", []), item.get("relevant", []), k)
        row.update({
            "id": item.get("id"),
            "type": item.get("type") or "unknown",
            "first_rank": first_relevant_rank(item.get("retrieved", []), item.get("relevant", []), k),
        })
        scored.append(row)

    by_type: dict[str, dict] = {}
    types = sorted({row["type"] for row in scored})
    for qtype in types:
        by_type[qtype] = _mean([row for row in scored if row["type"] == qtype])

    return {
        "n": len(scored),
        "overall": _mean(scored),
        "by_type": by_type,
        "per_query": scored,
    }
