"""SQLAlchemy 引擎工厂（SDD §6 桌面软件规格：SQLite WAL，代码层不绑死 SQLite 方言）。

- 默认指向 `.boss_profile/boss_state_sa.db`，与存量雷达库 `boss_state.db` 分开，
  由 Step 1.4 迁移 CLI 从旧库搬数（幂等）。
- `DB_BACKEND` 环境变量预留未来通道，本期只实现 sqlite。
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def _set_sqlite_pragma(dbapi_connection, _record):
    """连接级 PRAGMA：WAL + 外键（与存量 boss_state.get_db 对齐）。"""
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        pass


DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite")

_DEFAULT_DB_DIR = Path(__file__).resolve().parents[1] / ".boss_profile"
_DEFAULT_DB = _DEFAULT_DB_DIR / "boss_state_sa.db"

# `sa` 是 sqlite 的语义别名（SDD 1.3 开关用 `DB_BACKEND=legacy` 回退存量，其余走 SA）。
_SQLITE_BACKENDS = ("sqlite", "sa")


def get_db_path() -> Path:
    """返回数据库文件路径（sqlite/sa 后端）。"""
    if DB_BACKEND not in _SQLITE_BACKENDS:
        raise NotImplementedError(f"DB_BACKEND={DB_BACKEND} 本期未实施，仅支持 sqlite")
    override = os.environ.get("AI_PLATFORM_DB")
    if override:
        return Path(override)
    return _DEFAULT_DB


def get_engine(url: str | None = None) -> Engine:
    """构造 SQLite 引擎（WAL + FK）。供 Alembic 与适配层共用。"""
    if DB_BACKEND not in _SQLITE_BACKENDS:
        raise NotImplementedError(f"DB_BACKEND={DB_BACKEND} 本期未实施，仅支持 sqlite")
    if url is None:
        path = get_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{Path(path).as_posix()}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
    )
    if url and url.startswith("sqlite"):
        event.listen(engine, "connect", _set_sqlite_pragma)
    return engine
