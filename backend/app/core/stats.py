"""访客统计 + 每日配额。

为什么用 Redis 而不是数据库:
1. 配额是"高频写 + 到点自动过期"的场景,Redis 的 INCR 是原子操作,
   多进程/多 worker 并发时也不会把次数算错;数据库要靠行锁,又慢又重
2. 统计按天分区、到期自动删,不用写清理任务
3. 项目里本来就有 Redis(缓存、arq 队列),不引入新依赖

设计原则(很重要):
所有写入函数都 fail-open——Redis 挂了只让日志变多,绝不让问答主链路报错。
访客逻辑是"锦上添花",不该成为新的单点故障。

统计口径(看板上的每个数字都对应这里的一行):
PV(visits)      = 前端每次页面访问上报一次,同一访客 30 分钟内只算 1 次
UV(unique)      = 当日访问过的访客 ID 去重后的数量
questions       = 真正通过额度检查、开始生成回答的提问次数
byok_questions  = 其中"访客自带 Key"的部分,站长零成本
guest_questions = questions - byok_questions,站长买单的部分
repos           = 提交并成功入队的仓库数
今日/近7天/近30天 = 把日期范围内的每日数据相加;去重类指标用集合求并集,
                    所以"近7天UV"不等于"7天UV相加"(同一个人来 3 天只算 1 个)
"""

import re
# re:正则,用来校验访客 ID 的字符集
import uuid
# uuid:生成临时 key 的后缀,防止"求并集"的临时集合被并发请求互相覆盖
import redis as _redis
# 同步客户端,和 app/core/cache.py 保持同一种风格:
# 这些操作都是亚毫秒级,不值得为它们引入 await 样板代码
from datetime import datetime, timedelta, timezone
# datetime:取当前时间;timedelta:算时区偏移/日期回退/过期时长;timezone:时区对象
from fastapi import Request
# Request:FastAPI 的请求对象,拿不到访客 ID 时用它兜底成客户端 IP

from app.core.config import settings
# settings:读 redis_url 和 visit_dedup_seconds


# 北京时间。为什么不用 timezone.utc?
# 配额要按"自然日"重置,而站长和访客都在中国,
# 用 UTC 的话每天 08:00 才换天,用户会觉得"明明是今天却说我次数用完了"
_TZ = timezone(timedelta(hours=8))

# 模块级单例:进程序启动时建一次连接池,所有请求复用
# decode_responses=True:Redis 默认返回 bytes,这里让它自动转成 str,
# 免得每次读计数都要 int(x.decode("utf-8"))
_client = _redis.from_url(settings.redis_url, decode_responses=True)

# 统计数据的保留时长:400 天。
# 为什么要留这么久:看板要支持"近 30 天",而且留一年才能做同比;
# 这些 key 都是几个整数和几个小集合,400 天的存储成本可以忽略。
# (旧版本是 2 天,导致只能看今天,月报根本没有数据)
_STATS_KEEP_SECONDS = 400 * 24 * 60 * 60

# 配额 key 的保留时长:2 天就够了。
# 配额只关心"今天用了几次",留久了只会让 Redis 里堆一堆没用的计数器
_QUOTA_KEEP_SECONDS = 2 * 24 * 60 * 60

# 访客 ID 合法字符集:字母、数字、下划线、短横线。
# 为什么限制:访客 ID 会被拼进 Redis key 和日志,
# 如果不限制,传 "a:*" 之类就能用 SCAN 模式串去撞别人的 key
_VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _today() -> str:
    """返回北京时间的日期串,如 20261106。所有 key 都带它=天然的按天分区。"""
    return datetime.now(_TZ).strftime("%Y%m%d")


def _date_seq(days: int) -> list[str]:
    """返回最近 days 天(含今天)的日期串,按时间升序。
    例:days=7,今天 20261106 -> ['20261031', ..., '20261106']
    为什么升序:看板是从左到右按时间排,图表和列表都不需要再倒序。"""
    now = datetime.now(_TZ)
    # range(days-1, -1, -1):从"往前推 days-1 天"开始,一路退到 0(今天)
    return [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(days - 1, -1, -1)]


def get_visitor_id(request: Request, header_value: str | None) -> str:
    """确定"这次请求是谁",优先级:合法请求头 > 客户端 IP。
    为什么要 IP 兜底:前端 localStorage 被清、脚本直接调接口时都没有头,
    没有兜底就等于"没有配额",接口会被人白嫖。"""
    if header_value and _VISITOR_RE.match(header_value):
        # match 从字符串开头匹配,配合 ^...$ 等价于 fullmatch(整串必须合规)
        return header_value
    host = request.client.host if request.client else "unknown"
    # 前缀 ip: 用来和真实访客 ID 区分,统计时一眼能看出这是兜底身份
    return f"ip:{host}"


def _incr(key: str, amount: int = 1, ttl: int = _STATS_KEEP_SECONDS) -> int:
    """INCRBY 的薄封装:计数 +amount,并保证 key 有 TTL。
    为什么要 INCRBY 而不是 GET 再 SET:
    GET+SET 是两步,两个并发请求可能同时读到 99 再各自写 100,
    结果实际发了 101 次却只记了 100 次(竞态);
    INCRBY 在 Redis 内部是原子的,永远不会算错。"""
    value = _client.incrby(key, amount)
    if value == 1:
        # 只有第一次创建这个 key 时才设 TTL。
        # 每次都 expire 会导致 TTL 被无限续命,统计数据永远删不掉
        _client.expire(key, ttl)
    return int(value)


def record_visit(visitor_id: str) -> bool:
    """记一次访问。返回 True=这次真的计入了,False=去重窗口内的重复上报。

    去重怎么做的:
    先 SET NX 一个"访问凭证" key(nx=True 表示 key 不存在才写入),
    写成功说明这是窗口内的第一次 -> 才去给 PV+1、UV 集合加人;
    写失败说明窗口内已经来过 -> 直接返回 False,一个计数都不动。

    为什么用 Redis 而不是只靠前端 sessionStorage:
    前端只能挡住"同一个标签页刷新";换标签页、清缓存、用脚本直接打
    POST /demo/visit 都能绕过。服务端去重才是真正无法绕开的闸门。"""
    try:
        date = _today()
        seen_key = f"demo:visit_seen:{date}:{visitor_id}"
        # ex:窗口过期时间。到点自动删,下一个窗口的访问会重新计数
        first_visit = _client.set(seen_key, 1, nx=True, ex=settings.visit_dedup_seconds)
        if not first_visit:
            return False
        _incr(f"demo:visits:{date}")               # PV:去重后的访问次数
        _client.sadd(f"demo:uv:{date}", visitor_id) # UV:集合自动去重
        _client.expire(f"demo:uv:{date}", _STATS_KEEP_SECONDS)
        return True
    except Exception as exc:
        # fail-open:统计失败不影响访客使用
        print(f"[stats]记录访问失败:{exc}")
        return True


def record_repo(visitor_id: str) -> None:
    """记一次"提交仓库"行为,并记录是谁提交的(用于统计有多少人上传过仓库)。"""
    try:
        date = _today()
        _incr(f"demo:repos:{date}")
        _client.sadd(f"demo:repo_users:{date}", visitor_id)
        _client.expire(f"demo:repo_users:{date}", _STATS_KEEP_SECONDS)
    except Exception as exc:
        print(f"[stats]记录提交仓库失败:{exc}")


def record_question(visitor_id: str, byok: bool) -> None:
    """记一次问答。
    byok=True 表示这次回答花的是访客自己的 Key,站主零成本;
    分开统计是为了能算清"站主到底为演示掏了多少钱"。"""
    try:
        date = _today()
        _incr(f"demo:questions:{date}")                    # 总提问数
        if byok:
            _incr(f"demo:questions_byok:{date}")           # 其中自带 Key 的
        _client.sadd(f"demo:askers:{date}", visitor_id)    # 当日提问过的人
        _client.expire(f"demo:askers:{date}", _STATS_KEEP_SECONDS)
    except Exception as exc:
        print(f"[stats]记录提问失败:{exc}")


def consume_quota(kind: str, visitor_id: str, limit: int) -> bool:
    """消耗一次配额。返回 True=放行,False=今日额度已用完。
    kind:配额种类,如 "question"/"repo"/"test"。
    为什么每个种类单独一个 key:提问次数用完不应该连带禁止提交仓库,
    反过来也一样,三种额度必须互不干扰。"""
    try:
        key = f"demo:quota:{kind}:{_today()}:{visitor_id}"
        # 配额只留 2 天,和统计的长保留期分开
        used = _incr(key, ttl=_QUOTA_KEEP_SECONDS)
        return used <= limit
    except Exception as exc:
        # fail-open:Redis 故障时放行,宁可被刷也不让正常用户用不了
        print(f"[stats]配额检查失败,默认放行:{exc}")
        return True


def _num(value) -> int:
    """把 Redis 返回的字符串/None 统一成整数。
    mget 对"不存在的 key"返回 None,直接参与加法会 TypeError。"""
    return int(value or 0)


def _union_count(prefix: str, dates: list[str]) -> int:
    """统计日期范围内某个"去重集合"的总人数(UV / 提问人数 / 提交人数)。

    为什么不能把每天的 SCARD 相加:
    同一个人连续 3 天来访,每天各算 1,相加会得到 3,
    但"近3天独立访客"正确答案是 1。
    正确做法是集合求并集(SUNIONSTORE)后再 SCARD。

    临时 key 为什么要带 uuid:
    两个管理员同时打开看板,如果都用同一个临时 key,
    第二次 SUNIONSTORE 会覆盖第一次的结果,再互相 DELETE 就会报 key 不存在。
    每次请求一个独立临时 key,互不影响,用完立刻删。"""
    if len(dates) == 1:
        # 只有一天:直接数集合大小,省掉建临时 key 的两次往返
        return int(_client.scard(f"demo:{prefix}:{dates[0]}"))

    tmp_key = f"demo:tmp:{prefix}:{uuid.uuid4().hex}"
    keys = [f"demo:{prefix}:{d}" for d in dates]
    _client.sunionstore(tmp_key, keys)  # 把范围内所有日期的集合并到临时 key
    total = int(_client.scard(tmp_key))
    _client.delete(tmp_key)             # 立刻删,不留垃圾
    return total


def get_stats(days: int = 1) -> dict:
    """取统计快照:同时返回"周期汇总"和"每日明细"。

    参数 days:1=今日,7=近7天,30=近30天(接口层已限制 1~90)。
    返回结构:
    {
      "period": {"days":7,"start":"20261031","end":"20261106"},
      "totals": {...},   # 整个周期的汇总,看板顶部的大数字用它
      "daily":  [...]     # 每天的明细,天数>1 时画柱状图用
    }

    实现思路:
    计数类(PV/提问/仓库)是单值,直接 MGET 批量读,一次往返拿完 30 天;
    去重类(UV/人数)是集合,先 SCARD 每天(用 pipeline 批量),再对周期求并集。
    所有聚合都在"读取时"算,不需要定时任务——这就是"自动统计"的实现方式。
    """
    days = max(1, min(int(days), 90))
    dates = _date_seq(days)

    # ---- 计数类:一次 MGET 读一批,避免 30 天 × 4 类 = 120 次往返 ----
    visits_raw = _client.mget([f"demo:visits:{d}" for d in dates])
    questions_raw = _client.mget([f"demo:questions:{d}" for d in dates])
    byok_raw = _client.mget([f"demo:questions_byok:{d}" for d in dates])
    repos_raw = _client.mget([f"demo:repos:{d}" for d in dates])

    # ---- 去重类:用 pipeline 把 3*days 条 SCARD 压成一次往返 ----
    pipe = _client.pipeline()
    for d in dates:
        pipe.scard(f"demo:uv:{d}")        # 当日独立访客
        pipe.scard(f"demo:askers:{d}")    # 当日提问过的人数
        pipe.scard(f"demo:repo_users:{d}") # 当日提交过仓库的人数
    counts = pipe.execute()
    # pipeline 的结果是平铺的:第 i 天对应 counts[i*3]、[i*3+1]、[i*3+2]

    daily = []
    for i, date in enumerate(dates):
        q = _num(questions_raw[i])
        b = _num(byok_raw[i])
        daily.append({
            "date": date,
            "visits": _num(visits_raw[i]),
            "unique_visitors": int(counts[i * 3]),
            "questions": q,
            "byok_questions": b,
            "guest_questions": q - b,  # 站长买单的部分
            "repos": _num(repos_raw[i]),
            "askers": int(counts[i * 3 + 1]),
            "repo_submitters": int(counts[i * 3 + 2]),
            # 假设单次演示回答约 0.01 元,给出"约等于"的参考值(不是账单)
            "estimated_guest_cost_yuan": round((q - b) * 0.01, 2),
        })

    total_questions = sum(d["questions"] for d in daily)
    total_byok = sum(d["byok_questions"] for d in daily)
    totals = {
        "visits": sum(d["visits"] for d in daily),
        # 周期的 UV 必须用集合并集,不能把 daily 里的值相加(见 _union_count 注释)
        "unique_visitors": _union_count("uv", dates),
        "questions": total_questions,
        "byok_questions": total_byok,
        "guest_questions": total_questions - total_byok,
        "repos": sum(d["repos"] for d in daily),
        "askers": _union_count("askers", dates),
        "repo_submitters": _union_count("repo_users", dates),
        "estimated_guest_cost_yuan": round((total_questions - total_byok) * 0.01, 2),
    }

    return {
        "period": {"days": days, "start": dates[0], "end": dates[-1]},
        "totals": totals,
        "daily": daily,  # 升序:最左是范围内最早的一天
    }
