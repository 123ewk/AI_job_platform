"""SQLAlchemy 2.0 声明式模型（SDD §6）。

- 存量 7 表与 `boss_state.init_db()` 逐字段对齐（含后续 ALTER TABLE 追加列）。
- Agent 4 新表（agent_sessions/agent_steps/agent_tasks/approvals）字段按
  §4.1/§4.5 决策循环、后台任务、transcript、审批 需求设计，Step 2.1 补充状态机常量。
- 类型对齐：存量用 TIMESTAMP/DEFAULT CURRENT_TIMESTAMP，这里用
  `DateTime` + `server_default=func.current_timestamp()`，与 SQLite 语义一致；
  `sqlalchemy.sql.sqltypes` 避免绑死方言。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """声明式基类；`metadata` 供 Alembic autogenerate 使用。"""


# ══════════════════════════════════════
#  存量 7 表（逐字段对齐 boss_state）
# ══════════════════════════════════════


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_title: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str | None] = mapped_column(Text)
    salary: Mapped[str | None] = mapped_column(Text)
    job_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    city: Mapped[str | None] = mapped_column(Text)
    experience: Mapped[str | None] = mapped_column(Text)
    education: Mapped[str | None] = mapped_column(Text)
    hr_name: Mapped[str | None] = mapped_column(Text)
    hr_title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    greeting_text: Mapped[str | None] = mapped_column(Text)
    greeting_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # 公司去重 + HR 活跃度（CHANGES §1 §4）
    company_id: Mapped[str | None] = mapped_column(Text)
    brand_name: Mapped[str | None] = mapped_column(Text)
    hr_active_label: Mapped[str | None] = mapped_column(Text)
    hr_active_days: Mapped[int] = mapped_column(Integer, default=-1)
    # AI 24h 缓存（PR #3）
    optimize_result: Mapped[str | None] = mapped_column(Text)
    optimize_at: Mapped[datetime | None] = mapped_column(DateTime)
    chat_suggestion_result: Mapped[str | None] = mapped_column(Text)
    chat_suggestion_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    hr_name: Mapped[str] = mapped_column(Text, nullable=False)
    hr_company: Mapped[str | None] = mapped_column(Text)
    hr_title: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(Text)
    last_message_text: Mapped[str | None] = mapped_column(Text)
    last_message_from: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(Text, default="active")
    auto_reply_enabled: Mapped[int] = mapped_column(Integer, default=1)
    # 后续 ALTER TABLE 追加
    interest_level: Mapped[str | None] = mapped_column(Text)
    hr_wechat: Mapped[str | None] = mapped_column(Text)
    wechat_shared_at: Mapped[datetime | None] = mapped_column(DateTime)
    online_status: Mapped[str] = mapped_column(Text, default="")
    resume_sent: Mapped[int] = mapped_column(Integer, default=0)
    phone_shared: Mapped[int] = mapped_column(Integer, default=0)
    salary: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_status: Mapped[str | None] = mapped_column(Text)
    ai_generated: Mapped[int] = mapped_column(Integer, default=0)
    platform_time: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class DailyStat(Base):
    __tablename__ = "daily_stats"

    date: Mapped[str] = mapped_column(String, primary_key=True)
    applications_sent: Mapped[int] = mapped_column(Integer, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, default=0)
    messages_received: Mapped[int] = mapped_column(Integer, default=0)
    auto_replies_sent: Mapped[int] = mapped_column(Integer, default=0)


class Shortlist(Base):
    __tablename__ = "shortlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    job_title: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str | None] = mapped_column(Text)
    salary: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("name", "company_id", name="uq_companies_name_company_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    scale: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    employee_count: Mapped[str | None] = mapped_column(Text)
    founded: Mapped[str | None] = mapped_column(Text)
    open_positions: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


# ══════════════════════════════════════
#  Agent 4 新表（§4.1/§4.5/§4.7）
# ══════════════════════════════════════


class AgentSession(Base):
    """一次 Agent 对话会话（LangGraph thread 的持久化宿主）。"""

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    graph_thread_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    execution_mode: Mapped[str] = mapped_column(String, default="audit")
    status: Mapped[str] = mapped_column(String, default="active")
    user_prompt: Mapped[str | None] = mapped_column(Text)
    final_report: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class AgentStep(Base):
    """transcript 落库：LLM 决策、工具入参出参、审批记录，可完整回放。"""

    __tablename__ = "agent_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("agent_sessions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # plan / execute / approval / report / ask_user
    tool_name: Mapped[str | None] = mapped_column(String)
    tool_input: Mapped[dict | None] = mapped_column(JSON)
    tool_output: Mapped[dict | None] = mapped_column(JSON)
    llm_decision: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="done")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class AgentTask(Base):
    """后台长任务（send_greetings）：状态机 pending→running→completed|failed|interrupted|stopped。"""

    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("agent_sessions.id"))
    kind: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Approval(Base):
    """审计模式写操作审批记录。"""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("agent_sessions.id"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id"))
    step_id: Mapped[int | None] = mapped_column(ForeignKey("agent_steps.id"))
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    tool_input: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending / approved / rejected
    decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
