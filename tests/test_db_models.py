"""SDD Step 1.1：db/models.py + Alembic 基座验收单测。

覆盖（避免依赖真实 DB 文件，全部用临时目录/内存）：
- models.py 声明 11 张表（7 存量 + 4 Agent），表名与数量正确
- 存量表关键列与 legacy boss_state schema 语义对齐
- Agent 新表字段齐全（agent_sessions/agent_steps/agent_tasks/approvals）
- db.base 引擎工厂：SQLite、WAL、支持 AI_PLATFORM_DB 覆盖
- metadata.create_all 可建表（内存库）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect  # noqa: E402

from db import models  # noqa: E402
from db.base import DB_BACKEND, get_db_path, get_engine  # noqa: E402


def test_metadata_has_11_tables():
    names = set(models.Base.metadata.tables.keys())
    expect = {
        "applications",
        "conversations",
        "messages",
        "settings",
        "daily_stats",
        "shortlists",
        "companies",
        "agent_sessions",
        "agent_steps",
        "agent_tasks",
        "approvals",
    }
    assert names == expect


def test_application_columns_aligned_with_legacy():
    cols = {c.name for c in models.Application.__table__.columns}
    expect = {
        "id",
        "job_title",
        "company",
        "salary",
        "job_url",
        "city",
        "experience",
        "education",
        "hr_name",
        "hr_title",
        "description",
        "status",
        "greeting_text",
        "greeting_sent_at",
        "company_id",
        "brand_name",
        "hr_active_label",
        "hr_active_days",
        "optimize_result",
        "optimize_at",
        "chat_suggestion_result",
        "chat_suggestion_at",
        "created_at",
        "updated_at",
    }
    assert cols == expect
    assert models.Application.__table__.c.job_url.unique is True


def test_agent_tables_have_key_fields():
    assert {"id", "graph_thread_id", "execution_mode", "status"} <= {
        c.name for c in models.AgentSession.__table__.columns
    }
    assert {"id", "session_id", "kind", "tool_name", "tool_input", "tool_output"} <= {
        c.name for c in models.AgentStep.__table__.columns
    }
    assert {"id", "session_id", "kind", "params", "status", "progress_done"} <= {
        c.name for c in models.AgentTask.__table__.columns
    }
    assert {"id", "session_id", "tool_name", "tool_input", "status"} <= {
        c.name for c in models.Approval.__table__.columns
    }


def test_engine_is_sqlite_and_path_override(tmp_path, monkeypatch):
    assert DB_BACKEND == "sqlite"
    monkeypatch.setenv("AI_PLATFORM_DB", str(tmp_path / "custom.db"))
    db = get_db_path()
    assert db == tmp_path / "custom.db"


def test_create_all_on_memory():
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    assert len(tables) == 11
    assert "agent_tasks" in tables and "conversations" in tables


def test_get_engine_builds_and_journal_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PLATFORM_DB", str(tmp_path / "t.db"))
    eng = get_engine()
    with eng.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode == "wal"
