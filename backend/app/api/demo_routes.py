"""访客模式专用路由:站点配置 / 访问上报 / 测试自带 Key / 查看统计。

为什么单独一个文件而不是塞进 routes.py:
这四个接口都不做 RAG,不碰向量库,职责是"访客入口"——
单独成文件后,以后做运营后台(改配额、看趋势)可以继续往里加,不会污染问答主逻辑。
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
# APIRouter:迷你路由器,下面用 prefix="/demo"
# Depends:依赖注入,这里用来挂 get_current_user(统计接口要管理员)
# Header:声明"这个参数从请求头取",alias 指定真实头名
# HTTPException:校验失败时抛它,自动转成 4xx JSON
# Query:声明"这个参数从 URL 查询串取",并带上范围校验(统计天数)
# Request:拿客户端 IP,用于访客身份兜底

from pydantic import BaseModel, Field
# BaseModel:请求体模型;Field:给字段加长度约束

from app.core.config import settings
# 读 byok_enabled / 各配额数字,保证前端展示和实际限制用的是同一份配置

from app.core.byok import ByokError, parse_credentials, probe
# parse_credentials:校验访客填的地址/Key/模型;probe:真正发一次探测请求

from app.core.security import get_current_user
# 统计接口的守卫:只有管理员能看

from app.core.stats import (
    consume_quota,
    get_stats,
    get_visitor_id,
    record_visit,
)
# consume_quota:每日测试次数限制
# record_visit:访问上报
# get_stats:统计快照
# get_visitor_id:统一解析访客身份

router = APIRouter(prefix="/demo", tags=["demo"])
# tags:Swagger UI 里的分组名,和 repos/chat 区分开


class ByokReq(BaseModel):
    """POST /demo/test-llm 的请求体。
    为什么放在请求体而不是请求头:
    测试连接是"一次性动作",三个值只在这一次请求里用;
    而正式提问是"每次都要"的身份,才需要放在请求头里随每个请求走。"""

    base_url: str = Field(min_length=1, max_length=300, description="服务商地址,必须 https")
    api_key: str = Field(min_length=1, max_length=4096, description="访客自己的 Key")
    model: str = Field(min_length=1, max_length=100, description="模型名")


@router.get("/config", description="访客模式配置:前端据此决定是否显示自带Key入口")
async def demo_config():
    """【公开接口】返回站点当前的能力与额度。
    前端一进来就调它,做两件事:
    1) byok_enabled=false(公网HTTP阶段)时,把"自带Key"入口整个藏掉
    2) 把每日额度数字展示给访客,避免"问着问着突然被拦"的困惑
    为什么不直接在前端写死数字:额度是运营参数,改 .env 就能生效,
    前端写死的话每改一次都要重新构建发布。"""
    return {
        "byok_enabled": settings.byok_enabled,
        "guest_daily_question_limit": settings.guest_daily_question_limit,
        "byok_daily_question_limit": settings.byok_daily_question_limit,
        "guest_daily_repo_limit": settings.guest_daily_repo_limit,
        "demo_max_question_chars": settings.demo_max_question_chars,
    }


@router.post("/visit", description="上报一次访问(前端启动时调用)")
async def demo_visit(
    request: Request,
    x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
):
    """【公开接口】记录 PV / UV。
    为什么放在前端主动上报而不是中间件自动记:
    中间件会把每一个静态资源、每一条SSE心跳都算成一次"访问",
    数据会虚高一个数量级;由前端在页面挂载时调一次,才叫真实"人来过"。"""
    visitor = get_visitor_id(request, x_visitor_id)
    # record_visit 返回 False = 这次还在去重窗口内(30分钟),没有重复计数。
    # 把结果一起返回,前端/验收脚本可以据此确认"刷新没被重复算"
    counted = record_visit(visitor)
    return {"ok": True, "visitor_id": visitor, "counted": counted}


@router.post("/test-llm", description="测试访客自带 LLM Key 是否可用")
async def demo_test_llm(
    req: ByokReq,
    request: Request,
    x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
):
    """【公开接口,但有开关和频率限制】
    流程:总开关检查 -> 每日测试次数 -> 凭证校验 -> 探活。
    为什么探活在最后:前两步都是本地判断,几微秒就能拒绝,
    不该为明显非法的请求去外呼一次服务商。"""
    if not settings.byok_enabled:
        # 公网HTTP阶段直接关闭:Key 明文过网络的风险由站长来兜底,
        # 关掉功能是最简单的防护
        raise HTTPException(
            status_code=403,
            detail="本站当前未启用自带 Key 功能(需 HTTPS 就绪后开启)",
        )

    visitor = get_visitor_id(request, x_visitor_id)
    if not consume_quota("test", visitor, settings.byok_daily_test_limit):
        # 429 Too Many Requests:语义上比 403 更准确(不是没权限,是太频繁)
        raise HTTPException(
            status_code=429,
            detail=f"今日测试次数已用完(上限 {settings.byok_daily_test_limit} 次),请明天再试",
        )

    try:
        cred = parse_credentials(req.base_url, req.api_key, req.model)
    except ByokError as exc:
        # ByokError 的 message 是写给用户看的中文,原样返回
        raise HTTPException(status_code=400, detail=str(exc))

    ok, message = await probe(cred)
    # 不在这里抛 4xx:测试失败是"预期的业务结果",
    # 前端要把 message 显示在弹窗里让用户改配置,而不是当成请求异常
    return {"ok": ok, "message": message}


@router.get("/stats", description="访客数据看板:今日/近7天/近30天(仅管理员)")
async def demo_stats(
    days: int = Query(
        default=1,
        ge=1,
        le=90,
        description="统计天数:1=今日,7=近7天,30=近30天",
    ),
    _admin: str = Depends(get_current_user),
):
    """【管理员专用】查看 PV/UV/提问数/自带Key占比/预计花费。

    为什么用 days 参数而不是三个接口:
    聚合逻辑完全一致,只是日期范围不同;一个参数就能覆盖今日/周/月,
    前端切换标签时只改查询串,不用维护三条路由。

    返回值同时给"周期汇总(totals)"和"每日明细(daily)",
    看板顶部大数字用 totals,下面的柱状图直接用 daily,
    数据来自同一次查询,不会出现"总数和明细对不上"的情况。"""
    return get_stats(days)
