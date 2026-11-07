"""Week 2 验收:AST 切分产物体检。

原脚本硬编码了本机绝对路径且只是 print;改成 pytest 后可在任意机器执行。
没有本地仓库缓存时跳过(CI 不克隆仓库,避免测试依赖网络)。
"""

import ast
import textwrap
from pathlib import Path

import pytest

from app.rag.code_splitter import split_code_file
from app.rag.repo_loader import iter_code_file

CACHE = Path(__file__).parent / ".cache" / "requests"


@pytest.fixture(scope="module")
def chunks():
    if not CACHE.is_dir():
        pytest.skip(f"缺少本地仓库缓存 {CACHE},先跑 Week 3 的克隆步骤")
    result = []
    for path in iter_code_file(CACHE):
        result.extend(split_code_file(path, "requests", CACHE))
    assert result, "切分结果为空"
    return result


def test_every_chunk_has_locating_metadata(chunks):
    for chunk in chunks:
        meta = chunk["metadata"]
        assert meta["file_path"], "缺少 file_path"
        assert meta["start_line"] >= 1
        assert meta["end_line"] >= meta["start_line"]


def test_python_chunks_are_not_cut_in_half(chunks):
    bad = []
    for chunk in chunks:
        if chunk["metadata"]["language"] != "python":
            continue
        try:
            ast.parse(chunk["content"])
        except SyntaxError:
            try:
                ast.parse(textwrap.dedent(chunk["content"]))
            except SyntaxError as exc:
                bad.append((chunk["metadata"]["file_path"], chunk["metadata"]["start_line"], str(exc)))
    assert bad == [], f"存在被腰斩的块: {bad[:3]}"


def test_chunk_volume_is_reasonable(chunks):
    # 仅用于肉眼核对:requests 仓库当前约 261 块,数量骤变通常意味着切分退化
    assert 150 <= len(chunks) <= 600
