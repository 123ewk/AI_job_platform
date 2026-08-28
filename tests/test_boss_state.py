import os
from pathlib import Path
import tempfile

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boss_state
from boss_state import (
    _normalize_company_name,
    has_company_been_applied,
    add_application,
    update_application_status,
    clean_open_positions,
)


def setup_module(module):
    # Reset DB to a temp file for tests
    boss_state._local.conn = None
    tmp = Path(tempfile.gettempdir()) / "boss_state_test.db"
    if tmp.exists():
        tmp.unlink()
    boss_state.DB_PATH = tmp
    boss_state.init_db()


def test_normalize_company_name():
    assert _normalize_company_name("字节跳动有限公司") == "字节跳动"
    assert _normalize_company_name("阿里巴巴（中国）集团") == "阿里巴巴"
    assert _normalize_company_name(" 小米科技 ") == "小米科技"


def test_has_company_been_applied_exact_then_fuzzy():
    # Empty first
    r = has_company_been_applied("字节跳动")
    assert r["applied"] is False

    # Add an applied record
    aid = add_application(
        {
            "title": "AI 工程师",
            "company": "字节跳动",
            "url": "https://example.com/j1",
        }
    )
    update_application_status(aid, "applied")

    # Exact
    r2 = has_company_been_applied("字节跳动")
    assert r2["applied"] is True
    assert r2["count"] == 1

    # Suffix variant
    r3 = has_company_been_applied("字节跳动有限公司")
    assert r3["applied"] is True

    # Fuzzy — should not match to unrelated company
    r4 = has_company_been_applied("字节外包")
    assert r4["applied"] is False


def test_has_company_been_applied_with_company_id():
    # Add another company with company_id
    aid = add_application(
        {
            "title": "后端工程师",
            "company": "华为技术有限公司",
            "company_id": "huawei_123",
            "url": "https://example.com/j2",
        }
    )
    update_application_status(aid, "applied")

    # Exact by id
    r = has_company_been_applied("华为", "huawei_123")
    assert r["applied"] is True

    # Non-matching id
    r2 = has_company_been_applied("华为", "huawei_999")
    assert r2["applied"] is False


def test_clean_open_positions_filters_salary_and_ui_noise():
    raw = "5-7K、5-10K、3-5K、职位搜索、AI Agent开发工程师、电商运营、更多"
    positions, count = clean_open_positions(raw)
    assert count == 2
    assert positions == "AI Agent开发工程师、电商运营"
