"""问答历史会话:一个会话保存一轮或多轮问答,按访客身份隔离。"""

from datetime import datetime # created_at/updated_at 的时间类型

from sqlalchemy import DateTime, Index, Integer, String, func # 列类型与默认值
from sqlalchemy.dialects.postgresql import JSONB # 消息列表直接存 JSONB,避免额外建消息表
from sqlalchemy.orm import Mapped, mapped_column # SQLAlchemy 2.0 声明式映射

from app.core.db import Base # 所有模型共用的基类(启动时 create_all 会建表)


class ChatSession(Base):
  """一次问答页访问产生的历史会话。

  为什么用一张表把整段消息塞进 JSONB:
  1) 历史会话只需要"整段读、整段写",不需要按消息粒度查询;
  2) 避免再建 messages 表+外键,删除会话就是删一行,逻辑最简单。
  owner 是访客身份(前端 localStorage 的 visitor_id),
  同一浏览器才能看到自己的历史;换设备/清缓存后历史不可见,符合演示场景。
  """

  __tablename__ = "chat_sessions"
  # 联合索引:列表查询永远是 owner + repo_id 两个条件,一起索引命中最快
  __table_args__ = (Index("ix_chat_sessions_owner_repo", "owner", "repo_id"),)

  id: Mapped[int] = mapped_column(primary_key=True)
  repo_id: Mapped[int] = mapped_column(Integer, index=True)
  # owner 不加外键:访客没有用户表,是一个前端生成的随机 ID
  owner: Mapped[str] = mapped_column(String(64), index=True)
  title: Mapped[str] = mapped_column(String(120))
  # messages: [{"role":"user"/"assistant","content":...,"sources":[...]}]
  messages: Mapped[list] = mapped_column(JSONB, default=list)
  created_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), server_default=func.now()
  )
  # onupdate=func.now():每次 UPDATE 自动刷新,列表按它倒序就是"最新在上"
  updated_at: Mapped[datetime] = mapped_column(
    DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
  )
