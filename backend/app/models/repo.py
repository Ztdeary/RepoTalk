from datetime import datetime 
# Python标准库的日期时间类型，给created_at/updated_at当类型标注
from sqlalchemy import DateTime,String,Text,func # 列类型+数据库内置函数
from sqlalchemy.orm import Mapped,mapped_column
# Mapped——映射字段类型,用于在模型类里定义数据库字段
# mapper_column——声明"这一列的具体配置"的函数（类型、主键、默认值等）
from app.core.db import Base # 导入数据库基类,用于定义模型类

# 定义状态常量类
class RepoStatus:
    """仓库解析的状态机。Week 4的进度推送就按这些状态依次推进。
    刻意用普通字符串常量,而不是PostgreSQL的ENUM 类型:
    以后想加状态(比如"indexing")只改代码即可,不用写数据库迁移脚本。
    """
    # 为什么不直接用字符串字面量？防止拼错
    PENDING="pending" # 已提交，还没开工
    CLONING="cloning" # 正在git clone
    SPLITTING="splitting" # 正在CST切分
    EMBEDDING="embedding" # 正在向量化并写入pgvector
    DONE="done" # 全部完成，可以问答了
    FAILED="failed" # 中途失败，失败原因写进error字段

# 定义仓库模型类
class Repo(Base):
  """一个被解析的代码仓库。它的主键id就是贯穿全流程的repo_id。"""
  __tablename__="repos"
  # __tablename__ —— 双下划线属性，SQLAlchemy约定的魔法字段，指定数据库里真实的表名
  id:Mapped[int]=mapped_column(primary_key=True)
  # mapped_column(primary_key=True)是主键
  # SQLAlchemy看到int类型+主键，会自动用数据库的自增机制,不用写autoincrement
  repo_url:Mapped[str]=mapped_column(Text)
  # [str]->自动是NOT NULL
  # Text不限长度,存完整克隆地址;worker靠它去clone
  collection:Mapped[str]=mapped_column(String(128))
  # 对应的pgvector collection名,决定问答时去哪个向量库查，限制128个字符
  status:Mapped[str]=mapped_column(
    String(32),default=RepoStatus.PENDING,server_default=RepoStatus.PENDING
  )
  # default——Python侧默认值,server_default——数据库侧默认值
  # 双保险——哪怕直接往库里INSERT也不怕漏填
  error:Mapped[str|None]=mapped_column(Text) 
  # [str|None]->成功时这列是NULL，失败时存错误消息
  created_at:Mapped[datetime]=mapped_column(
    DateTime(timezone=True),server_default=func.now()
  )
  # DateTime(timezone=True)——带时区的时间戳,存的是UTC(全球协调时间基准)+时区信息
  # server_default=func.now()由数据库来填时间,避免应用服务器时钟不一致
  updated_at:Mapped[datetime]=mapped_column(
    DateTime(timezone=True),server_default=func.now(),onupdate=func.now()
  )
  # onupdate=func.now()—— UPDATE触发器:每次这行被UPDATE，数据库自动把这列刷成当前时间
  # 看仓库最近一次状态变更是什么时候
  
  
  
  