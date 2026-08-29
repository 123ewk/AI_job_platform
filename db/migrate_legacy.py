"""db/migrate_legacy.py — 存量库 → SQLAlchemy 库迁移（SDD Step 1.4），幂等。

把存量雷达库（`boss_state.py` 的 sqlite3 schema，默认 `.boss_profile/boss_state.db`）
逐行搬到 SQLAlchemy 库（Alembic schema，`db.base.get_engine()`，默认
`.boss_profile/boss_state_sa.db`）。7 张业务表字段已在 Step 1.2 逐字段对齐，这里只负责搬数。

设计要点：
- **保留主键**：逐行以显式 id 写入目标，维持 applications→conversations→messages 的
  外键关系；dashboard 按 id 引用应用的语义在迁移前后一致。
- **幂等**：目标表以 `INSERT OR IGNORE` 写入（按主键跳过已存在行），重复运行天然 no-op；
  目标 schema 缺失时用 `Base.metadata.create_all` 自建（与 alembic 初始迁移同 DDL，同样幂等）。
- **不动源库**：以只读模式 `mode=ro` 打开源库，迁移过程零写入。
- **不动 Agent 4 新表**：agent_sessions/agent_steps/agent_tasks/approvals 无存量对应，
  仅建 schema，不搬数。

列集不变式：迁移前逐一校验源库 7 张业务表列 ⊆ 目标 scaffold 表列，不满足即中止
（避免把目标放不下的列静默丢给 dashboard 造成缺字段）。

用法：
    python -m db.migrate_legacy                      # 默认源@.boss_profile/boss_state.db → SA 库
    python -m db.migrate_legacy --legacy path.db     # 指定源库
    python -m db.migrate_legacy --dry-run            # 只报告源库行数，不落库
    python -m db.migrate_legacy --schema-only        # 只建目标 schema 不搬数
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.engine import Engine

from db.base import get_db_path, get_engine
from db.models import Base

# 迁移顺序：父表先行，保持外键约束在事务内成立。
_TABLES = [
    "applications",
    "conversations",
    "messages",
    "settings",
    "daily_stats",
    "shortlists",
    "companies",
]

# 旧库默认路径 —— 与 boss_state.DB_PATH 对齐：`.boss_profile/boss_state.db`
_LEGACY_DEFAULT = Path(__file__).resolve().parents[1] / ".boss_profile" / "boss_state.db"


def _legacy_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _target_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1] for row in rows}


def _assert_parity(conn: sqlite3.Connection, engine: Engine) -> None:
    """源库列 ⊆ 目标 schema 列，否则中止迁移（列集不变式）。"""
    for table in _TABLES:
        missing = set(_legacy_columns(conn, table)) - _target_columns(engine, table)
        if missing:
            raise RuntimeError(
                f"[migrate] 目标表 {table} 缺源列 {sorted(missing)}，列集不一致，放弃迁移"
            )


def migrate(
    legacy_path: Optional[Path | str] = None,
    *,
    engine: Optional[Engine] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """从存量库搬数到 SA 引擎库，返回每表 {source, inserted, skipped} 统计。

    幂等：目标已存在的行（按主键）被 `INSERT OR IGNORE` 跳过；重复调用仅 `inserted=0`。
    """
    legacy_path = Path(legacy_path) if legacy_path else _LEGACY_DEFAULT
    if not legacy_path.exists():
        raise FileNotFoundError(f"[migrate] 存量库不存在: {legacy_path}")
    engine = engine or get_engine()

    # 目标 schema 缺失则自建（幂等；与 alembic 初始迁移同 DDL）
    Base.metadata.create_all(engine)

    src = sqlite3.connect(f"file:{legacy_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        _assert_parity(src, engine)

        stats: Dict[str, Dict[str, int]] = {}

        def _source_count(table: str) -> int:
            return src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

        if dry_run:
            for table in _TABLES:
                stats[table] = {"source": _source_count(table), "inserted": 0, "skipped": 0}
            return {"dry_run": True, "stats": stats}

        with engine.begin() as conn:
            for table in _TABLES:
                cols = _legacy_columns(src, table)
                if not cols:
                    stats[table] = {"source": 0, "inserted": 0, "skipped": 0}
                    continue
                col_sql = ",".join(f'"{c}"' for c in cols)
                ph = ",".join("?" for _ in cols)
                sql = f'INSERT OR IGNORE INTO "{table}" ({col_sql}) VALUES ({ph})'
                inserted = 0
                for row in src.execute(f'SELECT * FROM "{table}"').fetchall():
                    n = conn.exec_driver_sql(sql, tuple(row)).rowcount or 0
                    inserted += n
                total = _source_count(table)
                stats[table] = {"source": total, "inserted": inserted, "skipped": total - inserted}
        return {"dry_run": False, "stats": stats}
    finally:
        src.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="存量库 → SQLAlchemy 库迁移（幂等）")
    ap.add_argument("--legacy", help=f"存量 sqlite 库路径（默认 {_LEGACY_DEFAULT}）")
    ap.add_argument("--dry-run", action="store_true", help="只报告源库行数，不落库")
    ap.add_argument("--schema-only", action="store_true", help="只建目标 schema 不搬数")
    args = ap.parse_args()

    try:
        if args.schema_only:
            Base.metadata.create_all(get_engine())
            print(f"[migrate] 目标 schema 已就绪: {get_db_path()}")
            return 0

        result = migrate(args.legacy, dry_run=args.dry_run)
        stats = result["stats"]
        if result["dry_run"]:
            print("[migrate] 预演（不落库）源库行数：")
            for table, s in stats.items():
                print(f"  {table:<14} {s['source']}")
            return 0

        total_new = sum(s["inserted"] for s in stats.values())
        print(f"[migrate] 迁移完成 → {get_db_path()}")
        for table, s in stats.items():
            print(f"  {table:<14} 源 {s['source']:<4} 新写 {s['inserted']:<4} 跳过 {s['skipped']}")
        print(f"合计：{total_new} 行新写入（已有行全部跳过，幂等）")
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI 顶层统一兜底
        print(f"[migrate] 失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
