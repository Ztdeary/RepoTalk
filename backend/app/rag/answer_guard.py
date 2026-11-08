"""定位类问题的"查无此符号"兜底:仓库里没有定义就如实说没有。

为什么需要:
    仓库里不存在某个符号时,向量与 BM25 仍会返回一堆"提到这个词"的块
    (注释、测试、无关文件)。LLM 顺着这些块容易编出"它是如何被检索到的"
    这类解释,读者会误以为定义真的找到了。

策略(纯规则、零 LLM 成本、可单测):
    1. 只对"X 在哪定义 / 在哪个文件定义"这类定位问句生效;
    2. 从问句抽出目标符号(去反引号,取最后一段:`Session.send` -> `send`);
    3. 检查检索块里有没有真正的定义证据(def/class/function/struct ...);
    4. 没有 -> 直接回"当前仓库中没有找到 X 的定义",不调 LLM。
"""

import re

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*$")
_NOISE = re.compile(r"[`'\"“”]")
_SUFFIX_WORDS = ("函数", "方法", "类", "变量", "接口", "模块", "对象", "属性", "常量", "宏", "的")
_HINTS = ("在哪个文件", "在什么文件", "在哪", "定义在", "定义于")
# 定义写法模板。编译时统一加"行首锚点"(见 has_definition):
# 只有出现在一行的开头(允许缩进)才算定义,这样才能把测试用例里的
# 字符串字面量(如 ("def request(", "request", True)) 排除掉。
# 反例:测试文件里字符串形式的 def 通常前面还有引号/括号,不会顶到行首。
_DEF_TEMPLATES = (
    r"(?:async\s+)?def\s+{name}\b",
    r"class\s+{name}\b",
    r"(?:function|func|fn)\s+{name}\b",
    r"(?:struct|interface|enum|trait)\s+{name}\b",
    r"(?:const|let|var)\s+{name}\b",
    r"type\s+{name}\s+",
    # 模块级常量/类字段/字典键形式的声明,如 CANDIDATE_K=80、name: str = ""
    r"{name}\s*[:=]",
)


def extract_symbol(question: str) -> str | None:
    """从定位问句里抽目标符号;不是定位问句或抽不出合法标识符时返回 None。"""
    text = _NOISE.sub(" ", question)
    head = None
    for hint in _HINTS:
        index = text.find(hint)
        if index > 0:
            head = text[:index]
            break
    if head is None:
        return None

    for word in _SUFFIX_WORDS:
        head = head.replace(word, " ")
    tokens = [t for t in re.split(r"[\s,，。？?、:：]+", head) if t]
    if not tokens:
        return None
    token = tokens[-1].strip(".").split("(")[0]
    return token if _IDENT.match(token) else None


def _symbol_hits(metadata_symbol: str | None, target: str) -> bool:
    """元数据里的符号名是否就是目标符号(支持 Session.send 这种带前缀写法)。

    兼容 tree-sitter 切分时给 symbol 加的尺寸后缀,如 "hybrid_search (large)"。
    """
    meta = (metadata_symbol or "").strip().lower()
    if not meta:
        return False
    # 去掉 "(large)" / "(medium)" / "(small)" 这类尺寸后缀
    meta = re.sub(r"\s*\([^)]*\)\s*$", "", meta).strip()
    target = target.lower()
    return (
        meta == target
        or meta.endswith("." + target)
        or meta.split(".")[-1] == target
    )


def has_definition(docs: list, target: str) -> bool:
    """检索块里是否存在目标符号的真实定义,而不只是"提到"。

    两种证据都认:
    1. symbol 元数据等于目标符号(AST 切分产出的函数/类块);
    2. 正文里出现"行首的定义写法"(模块级常量、降级切分的块也能认)。
       为什么必须锚定行首:测试用例常把 "def request(" 当字符串字面量传参,
       那种写法前面有引号或括号,不会出现在行首,于是不会被误判成定义。
    """
    names = {target.lower(), target.lower().split(".")[-1]}
    patterns = [
        re.compile(r"^[ \t]*" + template.format(name=re.escape(name)), re.IGNORECASE | re.MULTILINE)
        for name in names
        for template in _DEF_TEMPLATES
    ]
    for doc in docs:
        if any(_symbol_hits(doc.metadata.get("symbol"), name) for name in names):
            return True
        content = doc.page_content or ""
        if any(pattern.search(content) for pattern in patterns):
            return True
    return False


def not_found_answer(question: str, docs: list) -> str | None:
    """命中兜底时返回话术,否则返回 None(交给正常 LLM 流程)。"""
    target = extract_symbol(question)
    if target is None or has_definition(docs, target):
        return None
    return (
        f"当前仓库中没有找到 `{target}` 的定义。\n\n"
        f"已检索 {len(docs)} 个代码块,均未出现 `def {target}` / `class {target}` "
        f"或符号名等于 `{target}` 的定义块。可能原因:该符号来自第三方依赖、"
        "位于当前仓库之外,或名称写法不同。"
    )
