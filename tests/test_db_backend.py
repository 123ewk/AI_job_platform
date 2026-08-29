"""Step 1.3 验收：`db/backend.py` DB_BACKEND 开关把雷达文件的 boss_state import 正确转发到当前后端。

红→绿：本文件先存在（红，db.backend 尚未实现），开关实现后绿。验收两件事：
  ①默认（sqlite/sa）绑定 SQLAlchemy 适配层 db.boss_state_sa；
  ②DB_BACKEND=legacy 回退存量 boss_state（§6 回退开关）。
模块级 `__getattr__` 转发，`from db.backend import X is 目标后端.X` 逐名一致。
"""

from __future__ import annotations

import importlib

import boss_state  # noqa: F811,N812  （存量模块，ruf 排除范围外）
import db.backend
from db import boss_state_sa


def _reload(monkeypatch, backend: str):
    """设 DB_BACKEND 并重载开关，返回用开关取到的 add_application 引用。"""
    monkeypatch.setenv("DB_BACKEND", backend)
    importlib.reload(db.backend)
    return db.backend.add_application


def test_default_sa(monkeypatch):
    """默认（未设/设 sqlite）应转发到 SQLAlchemy 适配层。"""
    impl = _reload(monkeypatch, "sqlite")
    assert impl is boss_state_sa.add_application


def test_alias_sa(monkeypatch):
    """`sa` 是 sqlite 别名，同样转发到适配层。"""
    impl = _reload(monkeypatch, "sa")
    assert impl is boss_state_sa.add_application


def test_legacy_fallback(monkeypatch):
    """DB_BACKEND=legacy 回退到存量 boss_state 模块（回退开关）。"""
    impl = _reload(monkeypatch, "legacy")
    assert impl is boss_state.add_application


def test_getattr_forward(monkeypatch):
    """模块级 __getattr__ 对任意公开名都转发到当前后端（不必枚举函数名）。"""
    monkeypatch.setenv("DB_BACKEND", "legacy")
    importlib.reload(db.backend)
    assert db.backend.get_setting is boss_state.get_setting
    assert db.backend.get_db is boss_state.get_db
    # 切回适配层后同一名字指向适配层实现
    _reload(monkeypatch, "sa")
    assert db.backend.get_setting is boss_state_sa.get_setting
