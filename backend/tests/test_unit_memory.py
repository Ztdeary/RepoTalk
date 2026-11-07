"""多轮上下文记忆:历史裁剪、prompt 片段、改写的触发条件。"""

from app.rag.memory import (
    HISTORY_EMPTY,
    build_history_block,
    normalize_history,
    should_rewrite,
)


def _turns(count: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条"}
        for i in range(count)
    ]


def test_empty_history_renders_placeholder():
    assert build_history_block([]) == HISTORY_EMPTY
    assert build_history_block(None) == HISTORY_EMPTY


def test_normalize_keeps_only_last_six_messages():
    kept = normalize_history(_turns(10))
    assert len(kept) == 6
    assert kept[0]["content"] == "第4条"


def test_normalize_drops_invalid_role_and_empty_content():
    kept = normalize_history(
        [
            {"role": "system", "content": "你是客服"},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "Session 在哪"},
            {"role": "assistant", "content": "requests/sessions.py"},
        ]
    )
    assert [m["role"] for m in kept] == ["user", "assistant"]


def test_normalize_truncates_each_message():
    kept = normalize_history([{"role": "user", "content": "a" * 500}], max_chars=200)
    assert len(kept[0]["content"]) == 200


def test_build_block_uses_plain_text_labels():
    block = build_history_block(
        [
            {"role": "user", "content": "request 在哪定义"},
            {"role": "assistant", "content": "requests/api.py"},
        ]
    )
    assert "用户: request 在哪定义" in block
    assert "助手: requests/api.py" in block
    # 纯文本而不是 JSON:历史里的花括号/引号不会被当成结构化数据
    assert "{" not in block and '"' not in block


def test_should_rewrite_needs_history():
    assert should_rewrite("它怎么处理超时?", []) is False


def test_should_rewrite_detects_pronoun_follow_up():
    history = [{"role": "user", "content": "Session.send 在哪定义"}]
    assert should_rewrite("它的超时参数默认值是多少?", history) is True
    assert should_rewrite("那 post 呢?", history) is True


def test_should_rewrite_skips_standalone_question():
    history = [{"role": "user", "content": "Session.send 在哪定义"}]
    assert should_rewrite("PreparedRequest.prepare 方法做了什么?", history) is False
