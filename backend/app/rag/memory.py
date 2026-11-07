"""多轮上下文记忆:纯函数,不调 LLM、不碰数据库。

职责边界:
- 本模块只做"判断要不要改写 / 截断历史 / 拼 prompt 片段";
- 真正的查询改写放在 pipeline.py(那里才有 _make_llm 和 BYOK 的 Key 选择逻辑)。
这样单测可以直接 import 本模块,不需要装 langchain。

设计取舍:
- 不落库、不建会话表。历史由前端随请求带上,后端每次现拼,
  既不引入迁移,也不会把用户对话留在服务端。
"""

# 默认上限来自配置(可用 .env 调整),函数参数仍可覆盖,方便单测传小值
from app.core.config import settings

# 最多带最近 6 条消息(3 轮问答)。再多就会挤占检索资料的 token 预算
MAX_HISTORY_MESSAGES = settings.history_max_messages
# 单条消息最多 200 字。历史只是"接上文"的线索,不需要整段回答
MAX_TURN_CHARS = settings.history_max_chars
# 没有历史时 prompt 里显示的占位符,保证模板变量永远非空
HISTORY_EMPTY = "(无)"

# 触发查询改写的信号词:出现这些词说明问题本身不完整,依赖上文才能检索
_REFERENCE_WORDS = (
    "它", "他", "她", "这个", "那个", "这些", "那些",
    "上面", "刚才", "前面", "上述", "该", "此",
)
# 追问常见的开头:即使句子较长,也是在延续上一轮
_FOLLOW_UP_PREFIXES = ("那", "还有", "另外", "换成", "如果是", "为什么", "怎么")

# 少于这个字数的问题几乎必然省略了主语,值得改写
_SHORT_QUESTION_CHARS = 10


def normalize_history(
    history: list | None,
    max_messages: int = MAX_HISTORY_MESSAGES,
    max_chars: int = MAX_TURN_CHARS,
) -> list[dict]:
    """把前端传来的历史裁剪成 [{"role": ..., "content": ...}]。

    兼容 dict 和对象两种形态(前端传 JSON,demo 脚本可能传对象),并做三件事:
    1. 只保留 user / assistant 两种角色,过滤掉前端可能混进来的系统消息;
    2. 丢掉空内容(流式中断留下的空 AI 气泡不应该进入 prompt);
    3. 只取最近 max_messages 条,单条截断到 max_chars 字。
    """
    if not history:
        return []

    cleaned: list[dict] = []
    for item in history:
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
        if role not in ("user", "assistant"):
            continue
        text = str(content or "").strip()
        if not text:
            continue
        cleaned.append({"role": role, "content": text[:max_chars]})

    return cleaned[-max_messages:]


def build_history_block(history: list | None) -> str:
    """把历史拼成 prompt 里的一个独立片段。

    格式刻意保持朴素(纯文本 + 角色前缀),不引入 JSON/XML:
    JSON 里的引号和花括号容易被模型当成"数据而不是对话",
    而纯文本配合模板里的"仅作参考"声明,既省 token 也更难被注入。
    """
    messages = normalize_history(history)
    if not messages:
        return HISTORY_EMPTY

    lines = []
    for message in messages:
        prefix = "用户" if message["role"] == "user" else "助手"
        lines.append(f"{prefix}: {message['content']}")
    return "\n".join(lines)


def should_rewrite(question: str, history: list | None) -> bool:
    """判断这轮问题是否值得花一次 LLM 调用去改写。

    为什么要判断而不是无脑改写:
    1. 首轮问题没有上文可依赖,改写只会改写;
    2. 每次改写都是一次真实计费的调用,而绝大多数问题本身就是完整的;
    3. 实测这条规则只在追问时才放行,能把额外成本压到很低。
    """
    if not normalize_history(history):
        return False

    text = question.strip()
    if not text:
        return False

    if len(text) <= _SHORT_QUESTION_CHARS:
        return True
    if any(word in text for word in _REFERENCE_WORDS):
        return True
    if text.startswith(_FOLLOW_UP_PREFIXES):
        return True
    return False
