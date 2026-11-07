"""指标库纯单测:不连数据库、不调模型,秒级完成。"""

import math

from tests.eval.metrics import (
    evaluate_queries,
    first_relevant_rank,
    hit_at_k,
    mrr_at_k,
    ndcg_at_k,
    normalize_path,
    recall_at_k,
    unique_paths,
)


def test_normalize_path():
    assert normalize_path(r"src\requests\api.py") == "src/requests/api.py"
    assert normalize_path("./src/requests/api.py") == "src/requests/api.py"


def test_unique_paths_dedup_keeps_order():
    assert unique_paths(["a.py", r"src\a.py", "b.py"]) == ["a.py", "src/a.py", "b.py"]
    assert unique_paths(["a.py", "a.py", "./a.py"]) == ["a.py"]


def test_recall_and_hit():
    retrieved = ["a.py", "b.py", "c.py"]
    assert recall_at_k(retrieved, ["b.py"], 3) == 1.0
    assert recall_at_k(retrieved, ["a.py", "c.py"], 3) == 1.0
    assert recall_at_k(retrieved, ["a.py", "c.py"], 2) == 0.5
    assert recall_at_k(retrieved, [], 3) == 0.0
    assert hit_at_k(retrieved, ["c.py"], 2) == 0.0
    assert hit_at_k(retrieved, ["c.py"], 3) == 1.0


def test_mrr_and_first_rank():
    retrieved = ["a.py", "b.py", "c.py"]
    assert first_relevant_rank(retrieved, ["c.py"], 3) == 3
    assert first_relevant_rank(retrieved, ["c.py"], 2) is None
    assert mrr_at_k(retrieved, ["b.py"], 3) == 0.5
    assert mrr_at_k(retrieved, ["z.py"], 3) == 0.0


def test_ndcg_binary():
    # 单相关文件排第 2:DCG=1/log2(3),IDCG=1
    assert ndcg_at_k(["a.py", "b.py"], ["b.py"], 2) == 1.0 / math.log2(3)
    # 两个相关文件排 1、3:DCG=1+1/log2(4),IDCG=1+1/log2(3)
    expected = (1.0 + 0.5) / (1.0 + 1.0 / math.log2(3))
    assert ndcg_at_k(["a.py", "x.py", "c.py"], ["a.py", "c.py"], 3) == expected
    assert ndcg_at_k(["a.py"], ["z.py"], 3) == 0.0


def test_duplicate_chunks_do_not_inflate():
    # 同一文件重复出现不应把 nDCG 抬高
    assert ndcg_at_k(["a.py", "a.py", "a.py"], ["a.py"], 3) == 1.0
    assert recall_at_k(["a.py", "a.py"], ["a.py"], 2) == 1.0


def test_evaluate_queries_grouping():
    per_query = [
        {"id": 1, "type": "loc", "retrieved": ["a.py", "b.py"], "relevant": ["a.py"]},
        {"id": 2, "type": "loc", "retrieved": ["b.py", "a.py"], "relevant": ["a.py"]},
        {"id": 3, "type": "usage", "retrieved": ["b.py"], "relevant": ["a.py"]},
    ]
    result = evaluate_queries(per_query, k=2)
    assert result["n"] == 3
    assert result["overall"]["mrr"] == (1.0 + 0.5 + 0.0) / 3
    assert result["by_type"]["loc"]["mrr"] == 0.75
    assert result["by_type"]["usage"]["mrr"] == 0.0
