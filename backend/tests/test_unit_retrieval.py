"""检索层纯逻辑:分词、RRF 融合、符号定义判定。"""

import pytest
from langchain_core.documents import Document

from app.rag.hybrid_retriever import _is_def_of, _tokenize, rrf_fuse


def test_tokenize_splits_camel_case_and_lowercases():
    tokens = _tokenize("getUserId")
    assert tokens == ["get", "user", "id"]


def test_tokenize_mixes_code_identifier_and_chinese():
    tokens = _tokenize("request 方法在哪里定义")
    assert "request" in tokens
    assert "定义" in tokens


def test_rrf_rewards_documents_hit_by_multiple_routes():
    a = Document(page_content="a", metadata={"file_path": "a.py", "start_line": 1})
    b = Document(page_content="b", metadata={"file_path": "b.py", "start_line": 2})
    fused = rrf_fuse([[a, b], [b]], k=60)
    assert [doc.metadata["file_path"] for doc, _ in fused] == ["b.py", "a.py"]
    # b 在两路里分别是第 2 名和第 1 名,分数累加; a 只有一路第 1 名
    assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1][1] == pytest.approx(1 / 61)


def test_rrf_deduplicates_same_chunk():
    a1 = Document(page_content="a", metadata={"file_path": "a.py", "start_line": 1})
    a2 = Document(page_content="a again", metadata={"file_path": "a.py", "start_line": 1})
    fused = rrf_fuse([[a1], [a2]], k=60)
    assert len(fused) == 1
    assert fused[0][0] is a1


@pytest.mark.parametrize(
    "symbol,ident,expected",
    [
        ("Session (large)", "Session", True),
        ("def request(", "request", True),
        ("async def send(", "send", True),
        ("class Session(SessionRedirectMixin):", "Session", True),
        ("response", "request", False),
    ],
)
def test_is_def_of_handles_both_symbol_formats(symbol, ident, expected):
    assert _is_def_of(symbol, ident) is expected
