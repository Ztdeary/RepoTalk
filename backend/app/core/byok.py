"""访客自带 LLM Key(BYOK)的校验与探活。

为什么单独一个文件:这里全是"不可信输入"的处理——
访客填的 base_url / api_key / model 会直接决定我们的服务器往哪里发请求。
所有校验规则集中一处,才不会出现"这个接口校验了、那个接口忘了"的漏洞。

三个核心函数:
1. parse_credentials():把三个请求头/表单字段校验成可信的 LLMCred
2. probe():向服务商发一个只花 1 token 的探测请求,判断 Key 是否真的能用
3. LLMCred:校验通过后的凭证对象,后续一路传到 pipeline 里构造 ChatOpenAI
"""

from dataclasses import dataclass
# dataclass:自动生成 __init__/__repr__ 的装饰器,省掉手写样板代码
# frozen=True 在下面用,让对象创建后不可修改,防止中途被意外改掉

from urllib.parse import urlparse
# urlparse:把 "https://api.deepseek.com/v1" 拆成
# scheme(协议)、netloc(域名端口)、path(路径) 三段,拆开才能逐段校验

import httpx
# httpx:支持 async 的 HTTP 客户端,项目里已装(0.28.1)
# 用 AsyncClient 而不是 requests:探测发生在 async 路由里,
# 同步请求会阻塞事件循环,一个访客测试连接就能卡住整个服务

from app.core.config import settings
# settings:读 byok_allowed_hosts / byok_test_timeout


class ByokError(ValueError):
    """凭证校验失败的专用异常。
    继承 ValueError:语义上就是"你传的值不合法";
    单独一个类是为了让调用方能精准捕获它、把 message 原样返回给前端。"""
    pass


@dataclass(frozen=True)
class LLMCred:
    """一份通过校验的 LLM 凭证。
    frozen=True:创建后字段只读,下游 pipeline 不可能把它改坏。"""
    base_url: str  # 已去掉尾部斜杠的地址,如 https://api.deepseek.com/v1
    api_key: str   # 访客的 Key 原文(只在内存里活一次请求的生命周期)
    model: str     # 模型名,如 deepseek-chat


def allowed_hosts() -> set[str]:
    """把逗号分隔的白名单字符串拆成一个集合。
    集合而不是列表:判断 "host in 集合" 是 O(1),
    以后白名单加到几十个域名也不会拖慢每个请求。"""
    # split(","):按逗号切成若干段
    # h.strip():去掉每段两边的空格(人在 .env 里手写常带空格)
    # .lower():域名大小写不敏感,统一小写再比较
    # if h.strip():过滤掉空段(比如末尾多了个逗号)
    return {
        h.strip().lower()
        for h in settings.byok_allowed_hosts.split(",")
        if h.strip()
    }


def _has_control_chars(value: str) -> bool:
    """检查字符串里有没有控制字符(换行 \n、回车 \r、制表 \t 等)。
    为什么必须查:这个 api_key 最终会进HTTP请求头。
    如果不拦截,攻击者传 "sk-xxx\r\nX-Admin: true" 就能往我们的外发请求里
    偷偷塞一个额外的头(HTTP 头注入);
    model 字段同理,有些服务商把它当字符串拼进请求体,也必须干净。"""
    # ord(ch) < 32:ASCII 里 0~31 全是控制字符(\n=10、\r=13、\t=9 都在内)
    # ord(ch) == 127:DEL,也算控制字符
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def parse_credentials(
    base_url: str | None,
    api_key: str | None,
    model: str | None,
) -> LLMCred:
    """把访客传来的三个值校验成 LLMCred,任一不合法就抛 ByokError。
    校验顺序:先看"有没有",再逐步验证"格式对不对",每一步都给出可行动的中文提示。"""
    # or "":None -> 空串,后面统一按字符串处理,避免到处判 None
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    model = (model or "").strip()

    # ---------- 1. 必填校验 ----------
    if not base_url:
        raise ByokError("缺少 X-LLM-Base-URL 请求头")
    if not api_key:
        raise ByokError("缺少 X-LLM-Key 请求头")
    if not model:
        raise ByokError("缺少 X-LLM-Model 请求头")

    # ---------- 2. 长度上限 ----------
    # 超长输入一律拒绝:既是防滥用,也避免把异常长的字符串写进日志/异常信息里
    if len(base_url) > 300:
        raise ByokError("Base URL 过长")
    if len(api_key) > 4096:
        raise ByokError("API Key 过长")
    if len(model) > 100:
        raise ByokError("模型名过长")

    # ---------- 3. 控制字符 ----------
    if _has_control_chars(api_key) or _has_control_chars(model):
        raise ByokError("Key 或模型名包含非法字符")

    # ---------- 4. 协议必须是 https ----------
    # 为什么强制 https:Key 放在请求头里,http 明文传输等于把 Key 广播给链路上所有人。
    # 只允许 https 就把"降级到明文"这条路堵死了
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise ByokError("Base URL 必须以 https:// 开头")

    # ---------- 5. 禁止 URL 里带用户名密码 ----------
    # 形如 https://user:pass@api.deepseek.com/v1
    # 这种写法的 hostname 是 api.deepseek.com,白名单能通过,
    # 但实际连接认证信息被塞进了 URL,属于绕过白名单的常见手法,直接拒绝
    if parsed.username or parsed.password:
        raise ByokError("Base URL 不能包含用户名或密码")

    # ---------- 6. 域名白名单(防 SSRF) ----------
    host = (parsed.hostname or "").lower()
    # parsed.hostname:只取主机名,自动去掉端口和 IPv6 方括号
    # 没有主机名(比如 https:///v1)直接拒
    if not host:
        raise ByokError("Base URL 缺少有效域名")
    if host not in allowed_hosts():
        raise ByokError(f"不允许的服务商域名:{host}")
    # 注意:这里不做 DNS 解析后二次校验,因为白名单里的都是公网知名域名;
    # 若将来要允许自定义域名,才需要额外做"解析结果不能是内网 IP"的校验

    # ---------- 7. 去掉尾部斜杠 ----------
    # 方便后面统一拼 /chat/completions;
    # 用 rstrip("/") 而不是 replace:它只削末尾,不会碰路径里面的斜杠
    base_url = base_url.rstrip("/")

    return LLMCred(base_url=base_url, api_key=api_key, model=model)


def _endpoint(cred: LLMCred) -> str:
    """拼出 OpenAI 兼容的对话补全地址。
    约定:访客填的 base_url 就是"根"(可以是 https://api.deepseek.com,
    也可以带 /v1),我们只在后面补 /chat/completions。
    这样 DeepSeek、OpenAI、Kimi、硅基流动等兼容服务全都能用同一条规则。"""
    return f"{cred.base_url}/chat/completions"


async def probe(cred: LLMCred) -> tuple[bool, str]:
    """发一个最小请求验证 Key 是否可用。
    返回 (是否成功, 给用户看的说明)。
    为什么用 max_tokens=1:只为验证鉴权和模型名,
    花 1 个 token 就够;不这样做,每次"测试连接"都可能产生真实的回答费用。"""
    url = _endpoint(cred)
    headers = {
        # Bearer 是 OpenAI 兼容协议的标准写法,所有目标服务商都认
        "Authorization": f"Bearer {cred.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cred.model,                              # 指定模型,顺便验证模型名是否存在
        "messages": [{"role": "user", "content": "ping"}], # 最小对话
        "max_tokens": 1,                                  # 只要一个 token
        "stream": False,                                  # 不要流式,一次性拿结果
    }

    try:
        # async with:请求结束自动关闭连接,不依赖 GC,避免连接泄漏
        async with httpx.AsyncClient(timeout=settings.byok_test_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        # 超时:地址填错/网络不通/服务商挂了的常见表现
        return False, "连接超时,请检查 Base URL 是否正确"
    except httpx.HTTPError as exc:
        # 连接失败、DNS 解析失败、TLS 证书错误等
        return False, f"连接失败:{exc}"

    # ---------- 按状态码给出可行动的提示 ----------
    status = resp.status_code
    if status == 200:
        return True, "连接成功"
    if status in (401, 403):
        return False, "Key 无效或无权限(401/403)"
    if status == 404:
        # 404 通常是 base_url 路径写错(少了 /v1)或模型名不存在
        return False, f"地址或模型不存在(404),请确认 Base URL 路径与模型名"
    if status == 429:
        return False, "该 Key 触发服务商限流(429),请稍后再试"
    # 其他 4xx/5xx:截取响应体前 200 字,方便用户和服务商侧对照排查
    # resp.text 可能是很长的 HTML,必须截断,否则会把前端弹窗撑爆
    return False, f"服务商返回 {status}:{resp.text[:200]}"
