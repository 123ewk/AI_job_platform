"""Boss State 后端开关（SDD 1.3 逐文件切换 import）。

`DB_BACKEND` 环境变量决定雷达各文件（boss_app / boss_automation /
boss_replier / boss_company）绑定的数据层实现：
  - `legacy` → 存量 `boss_state` 模块（迁移前的回退开关，§6，数据在 old schema）
  - 其他（默认 `sqlite`，含别名 `sa`）→ SQLAlchemy 适配层 `db.boss_state_sa`

用法：雷达文件不再直接 `from boss_state import …`，改从本模块取值：

    from db.backend import add_application, get_db, …

模块级 `__getattr__` 把所有未解析名字转发到当前后端，故不用枚举函数名，
新旧两后端 API 面一致（Step 1.2 差分单测保证），按名转发行为严格一致。
单点开关保证 DB_BACKEND=legacy 可整体回退，而非逐文件粒度。
"""

from __future__ import annotations

import os
import types

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite")


def _resolve() -> types.ModuleType:
    """按 DB_BACKEND 返回当前后端模块（惰性导入，避免无谓加载）。"""
    if DB_BACKEND == "legacy":
        import boss_state  # noqa: PLC0415  （存量模块，设计上按名导入）

        return boss_state
    import db.boss_state_sa  # noqa: PLC0415

    return db.boss_state_sa


_BACKEND: types.ModuleType = _resolve()


def __getattr__(name: str):
    """把未在本地定义的任何名字转发到当前后端模块。"""
    return getattr(_BACKEND, name)
