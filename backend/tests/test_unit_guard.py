"""定位类问题兜底:抽符号、判定义证据、拼话术。纯函数,不连库不调模型。"""

from types import SimpleNamespace

from app.rag.answer_guard import extract_symbol, has_definition, not_found_answer


def _doc(symbol, content):
    """构造一个只有 metadata/page_content 的最小文档对象。"""
    return SimpleNamespace(metadata={"symbol": symbol}, page_content=content)


def test_extract_symbol_from_plain_location_question():
    assert extract_symbol("request 函数在哪个文件定义？") == "request"


def test_extract_symbol_keeps_dotted_name():
    assert extract_symbol("`Session.send` 在哪定义?") == "Session.send"


def test_extract_symbol_takes_last_segment_of_method_question():
    assert extract_symbol("Session 类的 send 方法在哪个文件定义？") == "send"
    assert extract_symbol("PreparedRequest 的 prepare 方法在哪个文件定义？") == "prepare"


def test_extract_symbol_skips_usage_questions():
    assert extract_symbol("request 函数的 timeout 参数怎么用？") is None
    assert extract_symbol("Session 对象的 mount 方法是干什么的？") is None


def test_extract_symbol_skips_pronoun_only_question():
    assert extract_symbol("它在哪定义？") is None


def test_has_definition_accepts_real_definition_block():
    docs = [_doc("send", "def send(self, request, **kwargs):\n    pass")]
    assert has_definition(docs, "send")


def test_has_definition_accepts_dotted_target_via_last_segment():
    docs = [_doc("send", "def send(self):\n    pass")]
    assert has_definition(docs, "Session.send")


def test_has_definition_rejects_comment_only_mentions():
    docs = [_doc(None, "# 这里是 request 的调用说明,不是定义\nimport requests")]
    assert not has_definition(docs, "request")


def test_has_definition_accepts_js_and_go_style():
    assert has_definition([_doc(None, "function request(url) {}")], "request")
    assert has_definition([_doc(None, "const request = axios.create()")], "request")
    assert has_definition([_doc(None, "func request(url string) {}")], "request")


def test_not_found_answer_when_symbol_is_absent():
    docs = [_doc(None, "# 只是提到 request 这个词")]
    answer = not_found_answer("`request` 在哪定义?", docs)
    assert answer is not None
    assert "没有找到 `request` 的定义" in answer


def test_not_found_answer_is_none_when_definition_exists():
    docs = [_doc("request", "def request(method, url):\n    pass")]
    assert not_found_answer("`request` 在哪定义?", docs) is None


def test_not_found_answer_is_none_for_non_location_question():
    assert not_found_answer("request 怎么发 POST 请求?", []) is None
