"""AST 切分:元数据完整、Python 块语法自洽、未知语言有降级路径。"""

import ast
import textwrap
from pathlib import Path

import pytest

from app.rag.code_splitter import _simple_split, split_code_file

SAMPLE = '''"""模块文档"""

VERSION = "1.0"


class Demo:
    def method(self, value):
        return value * 2


def helper(name):
    if not name:
        return None
    return name.upper()
'''


@pytest.fixture()
def chunks(tmp_path):
    file_path = tmp_path / "sample.py"
    file_path.write_text(SAMPLE, encoding="utf-8")
    return split_code_file(file_path, "demo", tmp_path)


def test_chunks_expose_locating_metadata(chunks):
    assert chunks
    for chunk in chunks:
        meta = chunk["metadata"]
        assert meta["file_path"] == "sample.py"
        assert meta["language"] == "python"
        assert meta["start_line"] >= 1
        assert meta["end_line"] >= meta["start_line"]


def test_python_chunks_are_syntactically_whole(chunks):
    for chunk in chunks:
        content = chunk["content"]
        try:
            ast.parse(content)
        except SyntaxError:
            ast.parse(textwrap.dedent(content))


def test_class_and_function_symbols_are_found(chunks):
    symbols = " ".join(str(chunk["metadata"].get("symbol") or "") for chunk in chunks)
    assert "Demo" in symbols
    assert "helper" in symbols


def test_simple_split_fallback_keeps_metadata():
    # 缺少 grammar 时的降级路径:仍要产出带定位元数据的块,不能直接丢文件
    chunks = _simple_split("line of text\n" * 80, "plaintext", "demo", Path("notes.txt"))
    assert chunks
    assert all(chunk["metadata"]["file_path"] == "notes.txt" for chunk in chunks)
    assert all(chunk["metadata"]["language"] == "plaintext" for chunk in chunks)
