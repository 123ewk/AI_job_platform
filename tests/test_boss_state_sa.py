"""Step 1.2 验收：`boss_state`（存量 sqlite）与 `db.boss_state_sa`（SQLAlchemy 适配层）对同一组用例行为一致。

红→绿：本文件先存在（红，boss_state_sa 尚未实现），实现后绿。差分对比两套实现跑同一
顺向 scenario 电池的返回值快照必须逐项相等；这是规格 1.2 的验收：「新旧两套对同一组用例
行为一致」。
"""

from __future__ import annotations

import types

import boss_state as legacy  # noqa: N812  （存量模块，ruf 排除范围外）
from db import boss_state_sa as sa  # noqa: N812

# ──────────────────────────────────────────────────────────
#  两套数据层隔离 fixture
# ──────────────────────────────────────────────────────────


def _reset_legacy(monkeypatch, tmp_path):
    """让存量模块指向临时库：重定向 DB_PATH 并清空其线程连接的缓存，避免复用于导入时的真库。"""
    monkeypatch.setattr(legacy, "DB_PATH", tmp_path / "legacy.db")
    monkeypatch.setattr(legacy._local, "conn", None)
    legacy.init_db()
    return legacy


def _reset_sa(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PLATFORM_DB", str(tmp_path / "sa.db"))
    sa._reset()
    sa.init_db()
    return sa


# ──────────────────────────────────────────────────────────
#  scenario 电池：对任意一份「数据层接口」，依次执行一组有序操作，收集返回值快照
# ──────────────────────────────────────────────────────────


def run_battery(m: types.ModuleType) -> list:
    """跑一组覆盖公司/岗位/会话/消息/设置/统计/候选池的有序用例，返回 (label, value) 快照。

    电池是确定性的：两套库都以空表起步、按同一顺序执行，故而 pk 与相对时间戳可逐项相等。
    """
    snap: list = []

    def g(label, value):
        snap.append((label, value))

    # ── 公司去重 ──
    g("init_defaults", len(m.get_all_settings()) >= 20)
    j1 = {
        "title": "算法工程师", "company": "字节跳动有限公司", "salary": "30-40K", "url": "j1",
        "city": "北京", "experience": "3-5年", "education": "硕士",
        "hr_name": "李", "hr_title": "HRBP", "description": "d",
        "company_id": "bytedance", "brand_name": "字节跳动", "hr_active_label": "今日活跃", "hr_active_days": 1,
    }
    j2 = {"title": "后端开发", "company": "阿里巴巴（中国）集团", "url": "j2", "company_id": "alibaba"}
    j3 = {"title": "前端开发", "company": "小米科技", "url": "j3"}
    g("add_1", m.add_application(j1))
    g("add_2", m.add_application(j2))
    g("add_3", m.add_application(j3))
    g("has_by_company_id", m.has_company_been_applied("字节跳动", "bytedance"))
    g("has_fuzzy", m.has_company_been_applied("字节跳动"))
    g("has_prefix_mismatch", m.has_company_been_applied("字节"))
    g("has_not_applied", m.has_company_been_applied("不存在公司"))
    g("has_empty", m.has_company_been_applied("", ""))
    g("list_applied", m.list_applied_companies())

    # ── 公司在招字段清洗 ──
    g("clean_pos", m.clean_open_positions("5-7K、5-10K、AI Agent开发工程师、电商运营、更多、职位搜索"))
    g("clean_empty", m.clean_open_positions(""))

    # ── 公司信息缓存（24h TTL + UPSERT）──
    g("save_cmp_a", m.save_company_cache("某公司", "c1", industry="互联网", open_positions=["p1", "p2"]))
    g("get_cmp_by_id", m.get_cached_company("", "c1")["open_positions"])
    g("get_cmp_fresh", (m.get_cached_company("某公司", "c1") or {}).get("industry"))
    # 同键 upsert 刷新，应命中同一行
    m.save_company_cache("某公司", "c1", industry="AI")
    g("get_cmp_upserted", m.get_cached_company("某公司", "c1")["industry"])
    g("get_cmp_miss", m.get_cached_company("不存在公司"))
    g("get_cmp_ttl_zero", m.get_cached_company("某公司", "c1", max_age_hours=-1))

    # ── 岗位 CRUD 与状态 ──
    g("get_1", m.get_application(1)["job_title"])
    g("get_by_url", m.get_application_by_url("j2")["company"])
    m.update_application_status(1, "applied", greeting_text="你好")
    g("after_applied", m.get_application(1)["status"])
    g("today_applied_cnt", m.get_today_application_count())
    g("today_pending_cnt", m.get_today_pending_count())
    g("total_cnt", m.get_total_application_count())
    g("applied_cnt", m.count_applied_applications())
    g("filtered_cnt", m.count_filtered_applications())
    g("daily_limit_default", m.get_daily_limit())
    g("list_status", [d["status"] for d in m.list_applications(status="applied")])
    g("pend_list", [d["job_title"] for d in m.get_pending_applications()])
    # update_application_from_job：空值不覆盖旧值
    m.update_application_from_job(1, {"title": "算法专家", "salary": "", "company": ""})
    g("upd_nonempty", m.get_application(1)["job_title"])
    g("upd_empty_preserved_salary", m.get_application(1)["salary"])
    g("upd_empty_preserved_company", m.get_application(1)["company"])

    # ── 会话 ──
    conv1 = m.get_or_create_conversation(1, "李", "字节跳动", "算法工程师", "HRBP")
    g("conv_create", conv1)
    g("conv_dedupe", m.get_or_create_conversation(1, "李", "字节跳动", "算法工程师", "高级HRBP"))
    conv2 = m.get_or_create_conversation(2, "王", "阿里巴巴", "后端开发")
    g("conv_2", conv2)
    g("get_conv", m.get_conversation(conv1)["hr_name"])
    g("find_by_hr", m.find_conversation_by_hr_name("王")["hr_company"])
    g("list_active", len(m.list_active_conversations()))

    # ── 消息 ──
    g("msg_hr", m.add_message(conv1, "hr", "你好，方便聊聊吗"))
    g("msg_me", m.add_message(conv1, "me", "好的", ai_generated=True, delivery_status="sent"))
    g("get_messages_senders", [d["sender"] for d in m.get_messages(conv1)])
    g("recent_count", len(m.get_recent_messages(conv1, limit=1)))
    g("last_hr", m.get_last_hr_message(conv1)["content"])
    g("msg_exists", m.message_exists(conv1, "你好", "hr"))
    g("msg_not_exists", m.message_exists(conv1, "不存在", "hr"))

    # ── 会话交互字段 ──
    g("upd_last_nochange", m.get_conversation(conv1)["last_message_text"] is None)
    m.update_conversation_last_message(conv1, "你好，方便聊聊吗", "hr")  # 未变化，不应改 last_message_at/text
    g("last_still_none", m.get_conversation(conv1)["last_message_text"] is None)
    m.update_conversation_last_message(conv1, "你好，方便聊聊吗", "hr", unread_delta=1)
    g("unread_after", m.get_conversation(conv1)["unread_count"])
    m.update_conversation_last_message(conv1, "新的消息", "hr")
    g("last_updated", m.get_conversation(conv1)["last_message_text"])
    m.update_conversation_status(conv1, "active")
    m.update_conversation_interest(conv1, "high")
    g("interest", m.get_conversation(conv1)["interest_level"])
    m.update_conversation_wechat(conv1, "wxid_abc")
    g("wechat", m.get_conversation(conv1)["hr_wechat"])
    m.mark_resume_sent(conv1)
    m.mark_phone_shared(conv1)
    g("resume_sent", m.get_conversation(conv1)["resume_sent"])
    g("phone_shared", m.get_conversation(conv1)["phone_shared"])
    m.set_auto_reply(conv1, False)
    g("auto_reply_off", m.get_conversation(conv1)["auto_reply_enabled"])
    g("interest_high_cnt", m.count_interest_level("high"))
    g("wechat_exchanges", [d["hr_wechat"] for d in m.get_wechat_exchanges()])

    # ── replace_conversation_messages ──
    m.replace_conversation_messages(conv1, [
        {"sender": "me", "content": "好的", "status": "sent", "time": "2026-01-01"},
        {"sender": "hr", "content": "回复啦"},
    ])
    g("replace_senders", [d["sender"] for d in m.get_messages(conv1)])
    g("replace_ai_kept", [d["ai_generated"] for d in m.get_messages(conv1)])

    # ── 设置 / 统计 ──
    m.set_setting("ai_reply_style", "friendly")
    g("get_setting", m.get_setting("ai_reply_style"))
    g("get_setting_default", m.get_setting("nope", "def"))
    g("daily_limit", m.get_daily_limit())
    m.increment_daily_stat("messages_sent")
    m.increment_daily_stat("messages_sent")
    g("daily_stats", m.get_daily_stats())
    m.increment_daily_stat("applications_sent")

    # ── 候选池 ──
    g("short_add", m.add_to_shortlist("j5", "岗位X", "公司X", note="备注"))
    g("short_dup_returns_0", m.add_to_shortlist("j5", "岗位X", "公司X"))
    g("is_short", m.is_in_shortlist("j5"))
    g("short_list", [d["job_title"] for d in m.list_shortlists()])
    m.remove_from_shortlist(1)
    g("short_after_rm", m.is_in_shortlist("j5"))

    # ── 清空（放最后，破坏性）──
    g("home_reply_count", m.get_today_auto_reply_count())
    g("clear_conv_count", m.clear_all_conversations())
    g("clear_app_count", m.clear_all_applications())
    return snap


# ──────────────────────────────────────────────────────────
#  差分验收测试
# ──────────────────────────────────────────────────────────


def test_diff_legacy_vs_sa(tmp_path, monkeypatch):
    legacy_mod = _reset_legacy(monkeypatch, tmp_path)
    sa_mod = _reset_sa(monkeypatch, tmp_path)
    legacy_snap = run_battery(legacy_mod)
    sa_snap = run_battery(sa_mod)
    assert [label for label, _ in legacy_snap] == [label for label, _ in sa_snap]
    for (llabel, lval), (slabel, sval) in zip(legacy_snap, sa_snap):
        assert lval == sval, f"label={llabel}: legacy={lval!r} != sa={sval!r}"


def test_daily_stat_accumulates(tmp_path, monkeypatch):
    """increment_daily_stat 逐次累加，统计口径与存量一致。"""
    sa_mod = _reset_sa(monkeypatch, tmp_path)
    sa_mod.increment_daily_stat("messages_sent")
    sa_mod.increment_daily_stat("messages_sent")
    assert sa_mod.get_daily_stats()["messages_sent"] == 2
