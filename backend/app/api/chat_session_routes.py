"""问答历史会话接口:保存/列表/详情/删除。按访客身份隔离,无需登录。"""

from typing import Literal # role 只允许 user/assistant

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sa_delete, func, select # 列表/详情/删除都用得上
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.stats import get_visitor_id # 与配额/统计使用同一套访客身份规则
from app.models import ChatSession, Repo
from app.rag.pipeline import summarize_title # LLM 生成会话标题

router = APIRouter(prefix="/chat/sessions", tags=["chat-sessions"])


class MessageIn(BaseModel):
  """一条消息。sources 保留引用列表,重进会话时引用卡片还能展开。"""
  model_config = ConfigDict(extra="ignore")
  # extra="ignore":前端 ChatMessage 还带 id/done 等字段,后端用不上的直接忽略,
  # 不因为多字段就报 422

  role: Literal["user", "assistant"]
  content: str = ""
  sources: list[dict] | None = None
  error: str | None = None


class SessionSaveReq(BaseModel):
  repo_id: int
  messages: list[MessageIn] = Field(min_length=1)


class SessionUpdateReq(BaseModel):
  messages: list[MessageIn] = Field(min_length=1)


def _owner(request: Request, header: str | None) -> str:
  """取当前访客身份。和其他接口共用规则:请求头 > 客户端 IP。"""
  return get_visitor_id(request, header)


def _first_title_text(messages: list[MessageIn]) -> tuple[str, str]:
  """取用于生成标题的"第一个问题 + 第一条回答"。"""
  question = next(
    (m.content for m in messages if m.role == "user" and m.content.strip()), ""
  )
  answer = next(
    (m.content for m in messages if m.role == "assistant" and m.content.strip()), ""
  )
  return question, answer


def _fallback_title(question: str) -> str:
  """LLM 标题失败时的兜底:直接用问题开头,保证列表上不是空标题。"""
  text = " ".join(question.split())
  return text[:18] or "新会话"


def _to_summary(row) -> dict:
  """列表项:不带 messages 全文,只带计数,减小响应体。"""
  return {
    "id": row.id,
    "title": row.title,
    "created_at": row.created_at,
    "updated_at": row.updated_at,
    "message_count": int(row.message_count or 0),
  }


@router.get("", description="列出某仓库下当前访客的历史会话(最新在上)")
async def list_sessions(
  request: Request,
  repo_id: int = Query(..., description="仓库 id"),
  x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
  session: AsyncSession = Depends(get_session),
):
  """只返回当前访客自己的会话:换浏览器/清缓存后看不到别人的历史。"""
  owner = _owner(request, x_visitor_id)
  rows = (
    await session.execute(
      select(
        ChatSession.id,
        ChatSession.title,
        ChatSession.created_at,
        ChatSession.updated_at,
        func.jsonb_array_length(ChatSession.messages).label("message_count"),
      )
      .where(ChatSession.owner == owner, ChatSession.repo_id == repo_id)
      # updated_at 倒序=最新聊过的排最上面,符合"最新的在上"的要求
      .order_by(ChatSession.updated_at.desc())
    )
  ).all()
  return [_to_summary(r) for r in rows]


@router.get("/{session_id}", description="读取一个历史会话的完整消息")
async def get_session_detail(
  session_id: int,
  request: Request,
  x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
  session: AsyncSession = Depends(get_session),
):
  """带上完整 messages,前端点击历史会话后直接回填到聊天面板。"""
  owner = _owner(request, x_visitor_id)
  row = (
    await session.execute(
      select(ChatSession).where(
        ChatSession.id == session_id, ChatSession.owner == owner
      )
    )
  ).scalar_one_or_none()
  # 不属于当前访客的会话统一按"不存在"处理,避免暴露 id 是否存在
  if row is None:
    raise HTTPException(status_code=404, detail="会话不存在")
  return {
    "id": row.id,
    "repo_id": row.repo_id,
    "title": row.title,
    "messages": row.messages,
    "created_at": row.created_at,
    "updated_at": row.updated_at,
  }


@router.post("", description="保存一次问答会话,标题由 LLM 自动总结")
async def create_session(
  req: SessionSaveReq,
  request: Request,
  x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
  session: AsyncSession = Depends(get_session),
):
  """退出问答页时前端调用。

  标题生成失败不能影响会话保存:LLM 超时/限流时退化成"问题开头前 18 字",
  用户至少能在左侧历史列表里看到并重新打开这次问答。
  """
  owner = _owner(request, x_visitor_id)

  repo = (
    await session.execute(select(Repo).where(Repo.id == req.repo_id))
  ).scalar_one_or_none()
  if repo is None:
    raise HTTPException(status_code=404, detail=f"repo_id={req.repo_id} 不存在")

  messages = [m.model_dump(exclude_none=True) for m in req.messages]
  question, answer = _first_title_text(req.messages)
  try:
    title = await summarize_title(question, answer) if question else ""
  except Exception as exc:
    # 标题只是展示信息,失败不能挡住"保存历史"这个主流程
    print(f"[chat-session]标题生成失败: {exc}")
    title = ""
  title = (title or _fallback_title(question))[:40]

  row = ChatSession(
    repo_id=req.repo_id,
    owner=owner,
    title=title,
    messages=messages,
  )
  session.add(row)
  await session.commit()
  # commit 后再 refresh:拿到数据库生成的 id 与 created_at/updated_at
  await session.refresh(row)
  return {
    "id": row.id,
    "title": row.title,
    "created_at": row.created_at,
    "updated_at": row.updated_at,
    "message_count": len(messages),
  }


@router.put("/{session_id}", description="更新历史会话的消息(继续追问后退出时)")
async def update_session(
  session_id: int,
  req: SessionUpdateReq,
  request: Request,
  x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
  session: AsyncSession = Depends(get_session),
):
  """只更新消息,不重新生成标题:标题代表这次会话的主题,
  继续追问通常还是同一主题,重复调用 LLM 没有收益还会多花钱。"""
  owner = _owner(request, x_visitor_id)
  row = (
    await session.execute(
      select(ChatSession).where(
        ChatSession.id == session_id, ChatSession.owner == owner
      )
    )
  ).scalar_one_or_none()
  if row is None:
    raise HTTPException(status_code=404, detail="会话不存在")

  row.messages = [m.model_dump(exclude_none=True) for m in req.messages]
  # onupdate=func.now() 会自动刷新 updated_at,所以聊天过的最新会话会顶到列表最前
  await session.commit()
  await session.refresh(row)
  return {
    "id": row.id,
    "title": row.title,
    "created_at": row.created_at,
    "updated_at": row.updated_at,
    "message_count": len(row.messages),
  }


@router.delete("/{session_id}", description="删除一个历史会话")
async def delete_session(
  session_id: int,
  request: Request,
  x_visitor_id: str | None = Header(default=None, alias="X-Visitor-Id"),
  session: AsyncSession = Depends(get_session),
):
  """只能删自己的会话;删除不可恢复,前端会做二次确认。"""
  owner = _owner(request, x_visitor_id)
  result = await session.execute(
    sa_delete(ChatSession).where(
      ChatSession.id == session_id, ChatSession.owner == owner
    )
  )
  await session.commit()
  if result.rowcount == 0:
    raise HTTPException(status_code=404, detail="会话不存在")
  return {"ok": True, "id": session_id}
