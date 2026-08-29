"""Step 1.4 验收：`db/migrate_legacy.py` 幂等迁移，dashboard 数据迁移前后一致。

红→绿：本文件先存在（红，migrate_legacy 尚未实现），实现后绿。三条验收线：

1. **数据保全**（test_migrate_preserves_data）：用存量 `boss_state` 模块造一份带真实
   业务形态的临时旧库（含外键、活跃度列、缓存、候选池、companies 唯一约束），跑迁移后，
   逐表按主键读回，断言 7 张业务表「逐行、逐列」与源库完全相等 —— 这是「dashboard 各页
   面数据与迁移前一致」的底层保证。
2. **行为一致**（test_migrate_dashboard_consistent）：迁移后用 `db.boss_state_sa` 对同一份
   数据重放一串**只读** dashboard 口径函数（统计/列表/消息/候选池/公司聚合），快照与存量
   完全相等 —— 直接对应 dashboard 各 API 页面的数据来源。
3. **幂等**（test_migrate_idempotent）：重复迁移不产生重复行，第二次 `inserted=0`、计数不变。
"""

from __future__ import annotations

import sqlite3

import boss_state as legacy  # noqa: N812  （存量模块，ruff 排除范围外）
from db import boss_state_sa as sa  # noqa: N812
from db import migrate_legacy


def _make_legacy_db(tmp_path, monkeypatch) -> None:
    """用存量模块造一份临时旧库，注入真实业务形态的数据。"""
    monkeypatch.setattr(legacy, "DB_PATH", tmp_path / "legacy.db")
    monkeypatch.setattr(legacy._local, "conn", None)
    legacy.init_db()

    # ── 3 家公司、5 个岗位（含 company_id / 品牌 / HR 活跃度 / AI 缓存）──
    legacy.add_application({
        "title": "算法工程师", "company": "字节跳动", "salary": "30-40K", "url": "job/bytedance-1",
        "city": "北京", "experience": "3-5年", "education": "硕士", "hr_name": "李", "hr_title": "HRBP",
        "description": "负责推荐系统", "company_id": "bytedance", "brand_name": "字节跳动",
        "hr_active_label": "今日活跃", "hr_active_days": 1, "optimize_result": "{\"ok\":1}",
    })
    legacy.add_application({"title": "后端开发", "company": "字节跳动", "url": "job/bytedance-2", "company_id": "bytedance"})
    legacy.add_application({"title": "前端开发", "company": "小米科技", "url": "job/xiaomi-1"})
    legacy.add_application({"title": "数据科学", "company": "阿里", "url": "job/alibaba-1"})
    legacy.add_application({"title": "产品经理", "company": "阿里", "url": "job/alibaba-2", "city": "杭州"})

    # 状态流转（模拟投递过）
    legacy.update_application_status(1, "applied", greeting_text="您好，方便聊聊吗")
    legacy.update_application_status(2, "greeted")
    legacy.update_application_status(5, "offered")
    legacy.update_application_from_job(1, {"salary": "35-45K"})

    # ── 会话 + 消息（带外键，验证关系保留）──
    c1 = legacy.get_or_create_conversation(1, "李", "字节跳动", "算法工程师", "HRBP")
    legacy.update_conversation_interest(c1, "high")
    legacy.update_conversation_wechat(c1, "wxid_abc")
    legacy.set_auto_reply(c1, False)
    legacy.add_message(c1, "hr", "您好，方便聊一下吗？", delivery_status="sent")
    legacy.add_message(c1, "me", "好的，您好！", ai_generated=True, delivery_status="sent")
    c5 = legacy.get_or_create_conversation(5, "王", "阿里", "产品经理")
    legacy.add_message(c5, "hr", "约个面试？")

    # ── 设置 / 日统计 / 候选池 / 公司缓存 ──
    legacy.set_setting("ai_reply_style", "friendly")
    legacy.set_setting("greeting_mode", "smart")
    legacy.increment_daily_stat("messages_sent")
    legacy.increment_daily_stat("messages_sent")
    legacy.increment_daily_stat("applications_sent")
    legacy.add_to_shortlist("job/bytedance-2", "后端开发", "字节跳动", note="想投")
    legacy.save_company_cache("字节跳动", "bytedance", industry="互联网", scale="10000人以上", open_positions=["算法工程师", "后端开发"])


_DUMP_TABLES = [("applications", "id"), ("conversations", "id"), ("messages", "id"),
                ("settings", "key"), ("daily_stats", "date"), ("shortlists", "id"), ("companies", "id")]


def _dump(path) -> dict:
    """按主键读回整行，行内**按列名**组织为 {col: value}。

    用列名而非位置比对，是因为存量库与 SA 库的列**存储顺序不同**（存量经多次 ALTER
    TABLE 追加列，SA 模型列按声明序）——迁移按列名逐列拷贝，故比对必须忽略列序。
    """
    con = sqlite3.connect(path)
    out = {}
    for table, pk in _DUMP_TABLES:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        out[table] = {}
        for row in con.execute(f'SELECT * FROM "{table}"').fetchall():
            rec = dict(zip(cols, row))
            out[table][rec[pk]] = rec
    con.close()
    return out


def _reset_sa(tmp_path, monkeypatch, name="migrated.db"):
    # sa 由 AI_PLATFORM_DB 决定引擎（db.base.get_db_path 每次读取 env），重置即可切换目标库
    monkeypatch.setenv("AI_PLATFORM_DB", str(tmp_path / name))
    sa._reset()
    return str(tmp_path / name)


# ──────────────────────────────────────────────────────────
#  验收 1：数据保全 —— 迁后逐行逐列等于源库
# ──────────────────────────────────────────────────────────


def test_migrate_preserves_data(tmp_path, monkeypatch):
    _make_legacy_db(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    sa_path = _reset_sa(tmp_path, monkeypatch)

    result = migrate_legacy.migrate(legacy_path, engine=sa.get_engine())
    assert result["dry_run"] is False

    before = _dump(legacy_path)
    after = _dump(sa_path)
    assert before.keys() == after.keys()
    for table in before:
        assert before[table].keys() == after[table].keys(), f"主键集合不一致: {table}"
        for pk, legacy_row in before[table].items():
            assert after[table][pk] == legacy_row, f"{table}:{pk} 迁后与源不一致"


# ──────────────────────────────────────────────────────────
#  验收 2：dashboard 口径函数迁移前后一致（只读快照）
# ──────────────────────────────────────────────────────────


def _dashboard_snapshot(m: type) -> list:
    """跑一轮只读 dashboard 口径函数，返回 (label, value) 快照。"""
    snap = []

    def g(label, value):
        snap.append((label, value))

    g("total_cnt", m.get_total_application_count())
    g("applied_cnt", m.count_applied_applications())
    g("filtered_cnt", m.count_filtered_applications())
    g("today_applied", m.get_today_application_count())
    g("today_pending", m.get_today_pending_count())
    g("daily_limit", m.get_daily_limit())
    g("list_status", m.list_applications(status="applied"))
    g("pending_jobs", m.get_pending_applications())
    g("app_by_id", m.get_application(1))
    g("app_by_url", m.get_application_by_url("job/alibaba-1")["company"])
    g("applied_companies", m.list_applied_companies())
    g("has_company", m.has_company_been_applied("字节跳动", "bytedance"))
    g("conv_active", m.list_active_conversations())
    g("conv_messages", m.get_messages(1))
    g("conv_recent", m.get_recent_messages(1, limit=1))
    g("wechat_exchanges", m.get_wechat_exchanges())
    g("daily_stats", m.get_daily_stats())
    g("auto_reply_today", m.get_today_auto_reply_count())
    g("shortlists", m.list_shortlists())
    g("is_short", m.is_in_shortlist("job/bytedance-2"))
    g("cached_company", m.get_cached_company("字节跳动", "bytedance"))
    # 注：list_companies_by_position_count / list_jobs_by_company 是 SA 适配层独占的
    # 聚合函数（存量 boss_state 无），不属于「迁移前后」可比对象；其迁移后的正确性由
    # test_migrate_preserves_data（companies+applications 逐行列保全）+ Step 1.2 单测覆盖。
    return snap


def test_migrate_dashboard_consistent(tmp_path, monkeypatch):
    _make_legacy_db(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    _reset_sa(tmp_path, monkeypatch)  # 切换 SA 测试引擎（AI_PLATFORM_DB 指向临时库）

    migrate_legacy.migrate(legacy_path, engine=sa.get_engine())

    # 存量基准：直接对存量模块跑（DB_PATH 已是临时旧库）
    legacy_snap = _dashboard_snapshot(legacy)
    # 迁后口径：SA 适配层对「迁入的 SA 库」跑同一组只读函数
    sa_snap = _dashboard_snapshot(sa)

    assert [label for label, _ in legacy_snap] == [label for label, _ in sa_snap]
    for (llabel, lval), (slabel, sval) in zip(legacy_snap, sa_snap):
        assert lval == sval, f"label={llabel}: legacy={lval!r} != sa={sval!r}"


# ──────────────────────────────────────────────────────────
#  验收 3：幂等 —— 重复迁移不产生重复行
# ──────────────────────────────────────────────────────────


def test_migrate_idempotent(tmp_path, monkeypatch):
    _make_legacy_db(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    sa_path = _reset_sa(tmp_path, monkeypatch)

    first = migrate_legacy.migrate(legacy_path, engine=sa.get_engine())
    before = _dump(sa_path)

    # 第二次迁移：全部行已存在，应全部跳过、inserted=0
    second = migrate_legacy.migrate(legacy_path, engine=sa.get_engine())
    assert all(s["inserted"] == 0 for s in second["stats"].values()), "第二次迁移不应写入新行"
    assert all(s["skipped"] == s["source"] for s in second["stats"].values())

    after = _dump(sa_path)
    assert before == after, "重复迁移改变/重复了数据"
    # 再跑一次，三次仍稳定
    third = migrate_legacy.migrate(legacy_path, engine=sa.get_engine())
    assert all(s["inserted"] == 0 for s in third["stats"].values())

    # 首轮确有写入
    assert sum(s["inserted"] for s in first["stats"].values()) > 0


def test_migrate_dry_run_writes_nothing(tmp_path, monkeypatch):
    _make_legacy_db(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy.db"
    sa_path = _reset_sa(tmp_path, monkeypatch)

    result = migrate_legacy.migrate(legacy_path, engine=sa.get_engine(), dry_run=True)
    assert result["dry_run"] is True
    assert all(s["inserted"] == 0 for s in result["stats"].values())
    dump = _dump(sa_path)
    assert all(len(rows) == 0 for rows in dump.values()), "预演不应写入任何业务数据"


def test_migrate_missing_legacy_raises(tmp_path, monkeypatch):
    engine = sa.get_engine()
    # 不存在的源库 → FileNotFoundError
    try:
        migrate_legacy.migrate(tmp_path / "nope.db", engine=engine)
        raise AssertionError("应因源库不存在而抛 FileNotFoundError")
    except FileNotFoundError:
        pass
    # 校验导入路径存在（防未来被误删）
    assert migrate_legacy._LEGACY_DEFAULT.name == "boss_state.db"
