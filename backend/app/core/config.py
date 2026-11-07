# pydantic_settings:官方推荐的配置库,能把.env文件/环境变量自动读进Python对象
# BaseSettings:带"自动从环境读值"能力的模型基类
# SettingsConfigDict:配置类自身的配置(比如指定读哪个env文件)
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """全局配置:字段名会被自动映射成大写的环境变量名(大小写不敏感)。
    例如字段 llm_api_key 读的就是 .env 里的 LLM_API_KEY。
    字段全部没有默认值 -> .env 少写一个,进程启动时就直接报错,
    属于"启动即失败"的显式约束,比运行时才 KeyError 好排查得多。"""

    llm_api_key: str   # 站主自己的 DeepSeek Key:只读演示(访客不带Key)时用它
    llm_base_url: str  # 站主 LLM 的 OpenAI 兼容地址,如 https://api.deepseek.com
    llm_model: str     # 站主 LLM 的模型名,如 deepseek-chat

    embedding_api_key: str   # 向量化 Key(硅基流动):全站共用,访客永远不用自带
    embedding_base_url: str  # 向量化服务地址
    embedding_model: str     # 向量化模型名,如 BAAI/bge-m3(1024维)

    database_url: str  # PostgreSQL(pgvector)连接串
    redis_url: str     # Redis 连接串:缓存/任务队列/访客统计/配额都用它
    jwt_secret: str    # JWT 签名密钥:只给管理员登录用

    rerank_model: str   # 重排序模型,如 BAAI/bge-reranker-v2-m3
    admin_password: str # 管理员密码:登录后拥有"不受配额限制"的特权

    # ==================== 访客自带 LLM Key(BYOK)====================
    # byok_enabled 是总开关。为什么需要开关?
    # Key 走请求头明文传输,只有在 HTTPS 下才安全。
    # 本地测试/localhost 可以放心开;公网还是 HTTP 时(备案/证书没就绪)必须设 false,
    # 前端读到 false 会自动把"自带 Key"入口藏起来,访客根本看不到这个功能。
    # 写法 bool=True:默认开启,想关就在 .env 里写 BYOK_ENABLED=false
    byok_enabled: bool = True

    # 允许访客填的 LLM 服务商域名白名单,逗号分隔。
    # 为什么必须白名单:base_url 是访客可控的输入,如果不校验,
    # 坏人可以填 http://127.0.0.1:6379 或云厂商内网地址,让我们的服务器
    # 替他去请求内网服务(SSRF,服务端请求伪造),这是很典型的高危漏洞。
    # 注意只写域名,不写协议和路径
    byok_allowed_hosts: str = (
        "api.deepseek.com,api.openai.com,api.moonshot.cn,"
        "open.bigmodel.cn,api.siliconflow.cn,api.groq.com,"
        "openrouter.ai,api.together.xyz,dashscope.aliyuncs.com,"
        "ark.cn-beijing.volces.com"
    )

    # "测试连接"接口的超时秒数:只发 1 个 token 的探测请求,10 秒绰绰有余。
    # 不设上限的话,访客填一个黑洞地址就能长期占住我们的连接
    byok_test_timeout: int = 10

    # 每个访客每天最多点几次"测试连接"。
    # 防的是有人拿我们的服务器当"Key 有效性批量校验器"来刷(每测试一次都要外呼一次)
    byok_daily_test_limit: int = 10

    # ==================== 只读演示的成本控制 ====================
    # 没有自带 Key 的访客:只能用站主的 LLM 额度提问。
    # 每日问题上限:按 100 次/天算,配合下面 320 的输出上限,
    # 单次成本约 0.005~0.01 元,100 次也远低于一杯奶茶钱,更远低于"两毛一次"的红线
    guest_daily_question_limit: int = 100

    # 访客自带 Key 后的每日上限:不再花站主的钱,这个数字纯粹是防刷(防脚本无限打接口)
    byok_daily_question_limit: int = 500

    # 访客每天最多提交几个仓库。
    # 关键:解析用的仍是站主的 embedding 额度(访客的 Key 只覆盖"生成回答"),
    # 所以即使自带 Key 也必须限制提交数量,否则有人能刷爆向量化账单
    guest_daily_repo_limit: int = 3

    # 只读演示单次回答的最大输出 token 数:成本控制的"刹车片"。
    # 320 个 token 大约 200~250 个汉字,足够讲清一个函数;
    # 不设它,模型可能洋洋洒洒写 2000 token,单次成本直接翻好几倍
    demo_max_output_tokens: int = 320

    # 只读演示允许的问题最大字符数:防止有人把整篇文章粘进来当"问题",
    # 那样输入 token 会暴涨(输入也是要花钱的)
    demo_max_question_chars: int = 500

    # 只读演示参与拼接的上下文块数(top-k):块数越少输入越短、越便宜。
    # 检索阶段仍按 hybrid_search 的默认 top-k 取,这里只决定"喂给模型多少块"
    demo_context_k: int = 4

    # ==================== 多轮上下文记忆 ====================
    # 查询改写开关:默认关闭,先上线"历史透传"验证效果后再打开。
    # 打开后仅在"有历史 + 命中追问规则"时触发一次短 LLM 调用,
    # 首轮问题和独立问题完全不受影响,额外成本可控。
    enable_query_rewrite: bool = False

    # 历史注入上限:最近 3 轮问答(6 条消息)、单条 200 字。
    # 这是硬护栏:再长就会挤占检索资料的 token 预算,换来的接上文收益不划算。
    history_max_messages: int = 6
    history_max_chars: int = 200

    # ==================== 访客统计 ====================
    # 同一个访客在这个时间窗口内重复打开页面,只算 1 次访问(PV)。
    # 为什么需要它:PV 由前端在页面挂载时上报,用户按 F5 刷新就会重复 +1,
    # 一分钟刷 30 次就能把看板刷成"爆款"。1800 秒 = 30 分钟,
    # 既挡住刷新灌水,又不会把"上午来一次、下午再来一次"吞成一次。
    # 前端还有一层 sessionStorage 去重(同一标签页只上报一次),两层是互补的:
    # 前端挡自己人,后端挡脚本直接打接口。
    visit_dedup_seconds: int = 1800

    # 指定从哪个文件读取配置,ignore 表示 .env 里多出来的变量不报错
    # (开发机经常有一堆临时变量,严格校验只会让人烦)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings() # 实例化配置类,全局单例,别的地方import直接用
