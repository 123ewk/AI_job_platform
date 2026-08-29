"""SDD Step 3.1 + 3.2：query_jobs / get_progress 只读工具 + update_setting 写工具验收（红→绿，先红）。

本文件先存在（红，`agent/tools.py` / `agent.state.JobStatus` 尚未实现），实现后绿。
覆盖 §4.2 工具清单里的两个只读工具 + §3.1 验收点 + §3.2 update_setting 验收点：

1. **status 机映射**：Agent 岗位状态机（discovered/pending/greeted/applied/…）**直接写/读
   现有 `applications.status` 列**——`query_jobs(status="discovered")` 返回的就是
   `applications.status='discovered'` 的行，不另立平行列。
2. **`ungreeted` 专用过滤**（§3.1 单测焦点）：`query_jobs(ungreeted=true)` 只返回
   "可打招呼库存"（status ∈ {pending, discovered}），排除已打招呼/已投递/被过滤的行。
3. **参数校验（L3）**：未知 status、limit 越界、ungreeted 与 status 互斥 → 返回
   `{"error": ...}` 结果（回灌 LLM 自纠）而非抛异常打断决策环。
4. **get_progress**：今日已投（按 greeting_sent_at=今日）、daily_limit 设置、
   有效上限 `min(daily_limit, MAX_APPLY_PER_DAY)`、剩余额度、库存计数。
5. **注册与放行**：`default_registry(engine)` 含两个只读工具且 write=False（audit 直放）；
   经 AgentService 端到端跑一次 query_jobs，不留审批行。
6. **JobStatus 域完备**：单一真源非空无重复，GREETABLE ⊆ ALL，与既有状态词汇
   （pending/applied/replied/interview/filtered/greeted）逐一对齐。
7. **update_setting（§3.2）**：白名单（=手动设置 API `boss_app.SettingsUpdate` 字段集，
   测试钉死对齐）+ 敏感键（ai_api_key/wechat_id）**全模式拒绝**（autonomous 也拒，实现时定
   的最严解释——敏感键唯一可写路径是人工 `/api/settings`）+ `write=True` 走审批门。
8. **脱敏（§3.2/§4.3）**：`mask_sensitive` 对 `{key,value}` 命中敏感键的值、敏感键名值、
   独立手机号递归掩码；transcript（agent_steps 的 tool_input/llm_decision）不落原始密钥。
9. **FlowLock（§3.3/§4.6）**：浏览器互斥锁（threading 底座，跨 asyncio.to_thread 工作线程
   与事件循环线程）——带 owner 标签、阻塞获取排队、非阻塞 locked() 查询、幂等 release；
   **验收测试：FlowLock 被占时 search_jobs 排队而非并发**（锁释放前绝不碰浏览器）。
10. **search_jobs（§3.3/§4.2）**：读浏览器工具——调既有 `automation.search`（走 _run_pw
    桥注入）、max_pages 翻页（≤3）、入库 `status=discovered`、按 URL 去重、被过滤的恢复
    pending；L3 校验（缺 keyword / max_pages 越界 → error）；浏览器未启动 → error。
11. **get_conversations_summary（§3.3/§4.2）**：本地镜像库会话概览（不碰浏览器）——
    total/unread_total + 最近会话列表，last_message_text 手机号脱敏后才出工具。

mock/隔离：所有用例用内存 SQLite + StaticPool（`asyncio.to_thread` 跨线程 invoke 需
共享同一连接），插真实 `applications` / `settings` 行，工具走注入引擎、不碰真实库；
审批中断测试用 SqliteSaver 临时文件 + `Command(resume=...)`（进程重启恢复模式）；
浏览器工具注入假 automation + 直调 pw_runner（不启动 Playwright），FlowLock 用例
用独立锁实例避免污染模块单例。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import create_engine, func
from sqlalchemy.pool import StaticPool

from agent import service, state
from agent.flow_lock import FlowLock, default_flow_lock
from agent.graph import DEFAULT_RECURSION_LIMIT, build_agent_graph
from agent.service import AgentService
from agent.tools import get_conversations_summary_factory, search_jobs_factory
from db import models

# ══════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════


def _engine():
    # StaticPool + check_same_thread=False：AgentService.chat 用 asyncio.to_thread 在
    # 工作线程 invoke 图，内存库须所有线程共享同一连接（否则 :memory: 各连接独立丢表）。
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


def _insert_job(eng, *, title, status, city="上海", greeted_today=False, keyword=None):
    """插一行 applications；greeted_today=True 时 greeting_sent_at=今日（走 SQLite 本地时）。"""
    with eng.begin() as conn:
        conn.execute(
            models.Application.__table__.insert().values(
                job_title=title,
                company=f"{title}公司",
                job_url=f"https://zhaopin.example.com/{title}-{status}-{city}",
                city=city,
                status=status,
                description=keyword or f"{title} 岗位描述",
                greeting_sent_at=func.datetime("now", "localtime") if greeted_today else None,
            )
        )


def _set_setting(eng, key, value):
    with eng.begin() as conn:
        conn.execute(
            models.Setting.__table__.insert().values(key=key, value=value)
        )


# ══════════════════════════════════════════════════════════
#  验收 1：status 机映射到现有 applications.status
# ══════════════════════════════════════════════════════════


def test_query_jobs_status_maps_to_application_status():
    eng = _engine()
    _insert_job(eng, title="新搜AI岗", status=state.JobStatus.DISCOVERED)
    _insert_job(eng, title="存量待投", status=state.JobStatus.PENDING)
    _insert_job(eng, title="已打招呼", status=state.JobStatus.GREETED)
    _insert_job(eng, title="已投递", status=state.JobStatus.APPLIED)

    q = service.default_registry(eng).get("query_jobs")

    # Agent 状态值 == applications.status 列值：discovered 只回 discovered 行
    out_d = q.func(ungreeted=False, status=state.JobStatus.DISCOVERED)
    assert out_d["error"] is None
    assert [j["job_title"] for j in out_d["jobs"]] == ["新搜AI岗"]
    assert [j["status"] for j in out_d["jobs"]] == ["discovered"]

    # greeted 亦然
    out_g = q.func(ungreeted=False, status=state.JobStatus.GREETED)
    assert [j["job_title"] for j in out_g["jobs"]] == ["已打招呼"]


# ══════════════════════════════════════════════════════════
#  验收 2：ungreeted 专用过滤（§3.1 单测焦点）
# ══════════════════════════════════════════════════════════


def test_query_jobs_ungreeted_filter_only_greetable_inventory():
    eng = _engine()
    _insert_job(eng, title="待投A", status=state.JobStatus.PENDING)
    _insert_job(eng, title="待投B", status=state.JobStatus.DISCOVERED)
    _insert_job(eng, title="已打招呼", status=state.JobStatus.GREETED)
    _insert_job(eng, title="已投递", status=state.JobStatus.APPLIED)
    _insert_job(eng, title="已回复", status=state.JobStatus.REPLIED)
    _insert_job(eng, title="已面试", status=state.JobStatus.INTERVIEW)
    _insert_job(eng, title="被过滤", status=state.JobStatus.FILTERED)

    q = service.default_registry(eng).get("query_jobs")
    out = q.func(ungreeted=True)

    assert out["error"] is None
    titles = sorted(j["job_title"] for j in out["jobs"])
    # 只含可打招呼库存：pending + discovered；greeted/applied/replied/interview/filtered 排除
    assert titles == ["待投A", "待投B"]


def test_query_jobs_ungreeted_with_city_and_pagination():
    eng = _engine()
    _insert_job(eng, title="上海岗1", status=state.JobStatus.PENDING, city="上海")
    _insert_job(eng, title="上海岗2", status=state.JobStatus.PENDING, city="上海")
    _insert_job(eng, title="北京岗", status=state.JobStatus.PENDING, city="北京")
    _insert_job(eng, title="上海已投", status=state.JobStatus.APPLIED, city="上海")

    q = service.default_registry(eng).get("query_jobs")

    # ungreeted + 城市精确过滤
    out_city = q.func(ungreeted=True, city="上海")
    assert sorted(j["job_title"] for j in out_city["jobs"]) == ["上海岗1", "上海岗2"]

    # 分页：limit=1, offset=1 → 只回第二条
    out_page = q.func(ungreeted=True, city="上海", limit=1, offset=1)
    assert out_page["total"] == 2
    assert [j["job_title"] for j in out_page["jobs"]] == ["上海岗2"]


# ══════════════════════════════════════════════════════════
#  验收 3：参数校验（L3，校验失败回灌而非抛异常）
# ══════════════════════════════════════════════════════════


def test_query_jobs_rejects_unknown_status():
    eng = _engine()
    q = service.default_registry(eng).get("query_jobs")
    out = q.func(status="hacked")
    assert out["error"]
    assert "hacked" in out["message"]


def test_query_jobs_rejects_ungreeted_with_status_conflict():
    eng = _engine()
    q = service.default_registry(eng).get("query_jobs")
    out = q.func(ungreeted=True, status=state.JobStatus.PENDING)
    assert out["error"]
    assert "互斥" in out["message"]


def test_query_jobs_rejects_bad_limit():
    eng = _engine()
    q = service.default_registry(eng).get("query_jobs")
    out = q.func(limit=0)
    assert out["error"]  # pydantic ge=1 拦截，返回 error dict 而非抛异常


# ══════════════════════════════════════════════════════════
#  验收 4：get_progress
# ══════════════════════════════════════════════════════════


def test_get_progress_reports_today_and_remaining_quota():
    eng = _engine()
    # 今日已投 3（greeting_sent_at=今日）；昨日已投 1（不计数）
    for i in range(3):
        _insert_job(eng, title=f"今日投{i}", status=state.JobStatus.APPLIED, greeted_today=True)
    _insert_job(eng, title="昨日投", status=state.JobStatus.APPLIED)
    _insert_job(eng, title="待投", status=state.JobStatus.PENDING)
    _set_setting(eng, "daily_apply_limit", "10")

    g = service.default_registry(eng).get("get_progress")
    out = g.func()

    assert out["today_applied"] == 3
    assert out["daily_limit"] == 10
    assert out["remaining"] == 7  # 10 - 3
    assert out["ungreeted_count"] == 1
    assert out["pending_count"] == 1


def test_get_progress_effective_limit_capped_at_hard_limit():
    eng = _engine()
    _insert_job(eng, title="今日投1", status=state.JobStatus.APPLIED, greeted_today=True)
    _set_setting(eng, "daily_apply_limit", "200")  # 配置超过硬上限

    g = service.default_registry(eng).get("get_progress")
    out = g.func()

    # 有效上限 = min(daily_limit, MAX_APPLY_PER_DAY=50) → 剩余 50 - 1 = 49
    assert out["daily_limit"] == 200
    assert out["effective_limit"] == 50
    assert out["remaining"] == 49


# ══════════════════════════════════════════════════════════
#  验收 5：注册与 audit 直放（只读工具不留审批行）
# ══════════════════════════════════════════════════════════


def test_default_registry_includes_read_tools_as_readonly():
    eng = _engine()
    reg = service.default_registry(eng)
    names = set(reg.names())
    assert {"query_jobs", "get_progress"} <= names
    assert reg.get("query_jobs").write is False
    assert reg.get("get_progress").write is False


def test_graph_audit_executes_query_jobs_without_approval():
    eng = _engine()
    _insert_job(eng, title="待投端到端", status=state.JobStatus.PENDING)
    _insert_job(eng, title="已打招呼端到端", status=state.JobStatus.GREETED)

    tool_outputs: list[dict] = []

    def _planner(messages, tool_schemas):
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            tool_outputs.append(json.loads(tool_msgs[-1]["content"]))
            return {"action": "report", "content": "库存已查"}
        return {"action": "tool", "name": "query_jobs", "arguments": {"ungreeted": True}}

    svc = AgentService(engine=eng, make_planner=lambda ui: _planner)

    async def _chat():
        return await svc.chat("帮我看看有没有没打招呼的岗位", "t-tools", "audit")

    result = asyncio.run(_chat())
    assert result["report"] == "库存已查"
    assert result["status"] == "completed"

    # 工具输出已回灌 planner：只含可打招呼库存
    payload = tool_outputs[0]["output"]
    assert sorted(j["job_title"] for j in payload["jobs"]) == ["待投端到端"]

    # 只读工具在 audit 直接放行，不留审批行
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approvals").scalar() == 0


# ══════════════════════════════════════════════════════════
#  验收 6：JobStatus 域完备（单一真源，与存量状态词汇对齐）
# ══════════════════════════════════════════════════════════


def test_job_status_domain_well_formed():
    all_ = state.JobStatus.ALL
    assert all_
    assert len(all_) == len(set(all_))
    # GREETABLE 是 ALL 的子集，且与"已打招呼/已投递"集合不相交
    assert state.JobStatus.GREETABLE <= all_
    assert state.JobStatus.GREETABLE.isdisjoint(state.JobStatus.PROGRESSED)
    # 与存量 applications.status 词汇逐一对齐（boss_state 默认 pending、去重 applied_status）
    assert state.JobStatus.PENDING == "pending"
    assert {"applied", "replied", "interview"} <= state.JobStatus.PROGRESSED
    assert state.JobStatus.GREETED == "greeted"
    assert state.JobStatus.DISCOVERED == "discovered"


# ══════════════════════════════════════════════════════════
#  验收 7（Step 3.2）：update_setting 写工具
# ══════════════════════════════════════════════════════════


def test_update_setting_rejects_non_whitelist_key():
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")
    u = service.default_registry(eng).get("update_setting")

    out = u.func(key="evil_key", value="1")

    assert out["error"]
    assert "白名单" in out["message"]
    assert out["allowed"] and "evil_key" not in out["allowed"]
    with eng.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM settings WHERE key='evil_key'"
        ).scalar() == 0  # 拒绝后不落库


def test_update_setting_rejects_sensitive_key_in_all_modes():
    """敏感键（ai_api_key/wechat_id）全模式拒绝：工具层即拦，autonomous 也不放过（§3.2）。"""
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")
    u = service.default_registry(eng).get("update_setting")

    for sensitive in sorted(state.SENSITIVE_SETTING_KEYS):
        out = u.func(key=sensitive, value="sk-super-secret")
        assert out["error"], f"{sensitive} 应被拒绝"
        assert "敏感" in out["message"]
        with eng.connect() as conn:
            assert conn.exec_driver_sql(
                "SELECT COUNT(*) FROM settings WHERE key=?", (sensitive,)
            ).scalar() == 0  # 拒绝后不落库


def test_update_setting_writes_whitelisted_key():
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")
    u = service.default_registry(eng).get("update_setting")

    out = u.func(key="daily_apply_limit", value="20")

    assert out["error"] is None
    assert out["key"] == "daily_apply_limit"
    with eng.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT value FROM settings WHERE key='daily_apply_limit'"
        ).scalar() == "20"


def test_update_setting_inserts_missing_key():
    eng = _engine()
    u = service.default_registry(eng).get("update_setting")

    out = u.func(key="title_filter_keywords", value="算法,AI")

    assert out["error"] is None
    with eng.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT value FROM settings WHERE key='title_filter_keywords'"
        ).scalar() == "算法,AI"


def test_whitelist_aligns_with_manual_settings_api():
    """§4.2/§3.2：Agent 白名单 == 手动设置 API 的 SettingsUpdate 字段集——复用同一白名单。"""
    from boss_app import SettingsUpdate  # noqa: PLC0415  # 懒加载：boss_app 顶层仅注册路由，副作用都在 startup 内

    assert state.SETTINGS_WHITELIST == set(SettingsUpdate.model_fields)
    # 敏感键是白名单子集：白名单通过、敏感键再被工具层硬拒
    assert state.SENSITIVE_SETTING_KEYS <= state.SETTINGS_WHITELIST


def test_default_registry_includes_update_setting_as_write():
    reg = service.default_registry(_engine())
    assert "update_setting" in reg.names()
    assert reg.get("update_setting").write is True


def test_update_setting_rejects_missing_key_param():
    u = service.default_registry(_engine()).get("update_setting")
    out = u.func(value="20")  # key 是必填（Pydantic require）
    assert out["error"]


# ══════════════════════════════════════════════════════════
#  验收 8（Step 3.2）：脱敏 mask_sensitive
# ══════════════════════════════════════════════════════════


def test_mask_sensitive_covers_setting_secrets_and_phone():
    m = state.mask_sensitive
    # {key, value} 结构：key 命中敏感设置键 → value 掩码
    assert m({"key": "ai_api_key", "value": "sk-abc"}) == {"key": "ai_api_key", "value": "***"}
    assert m({"key": "wechat_id", "value": "wxid_1"}) == {"key": "wechat_id", "value": "***"}
    # 普通设置键原样保留
    assert m({"key": "daily_apply_limit", "value": "20"}) == {"key": "daily_apply_limit", "value": "20"}
    # 敏感键名的值掩码（键名保留便于定位）
    assert m({"ai_api_key": "sk-abc"}) == {"ai_api_key": "***"}
    assert m({"wechat_id": "wxid_1"}) == {"wechat_id": "***"}
    # 手机号走 log_config 既有格式（保留前3后4，Step 0.3 单真源）
    assert m("联系 13800138000 结尾") == "联系 138****8000 结尾"
    # 游离 sk-token / Bearer 委托 log_config（保留首尾）
    assert m("Bearer sk-abcdef123456") == "Bearer sk-a*****456"
    # 嵌套结构递归
    assert m({"args": {"key": "ai_api_key", "value": "sk-abc"}}) == {"args": {"key": "ai_api_key", "value": "***"}}
    # 非敏感内容原样
    assert m("算法岗位描述") == "算法岗位描述"
    assert m([1, 2, None, True]) == [1, 2, None, True]


# ══════════════════════════════════════════════════════════
#  验收 9（Step 3.2）：update_setting 走图——autonomous 直写 / 敏感键拒 / 审批门
# ══════════════════════════════════════════════════════════


def test_autonomous_update_setting_executes_directly_no_approval():
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")

    def _planner(messages, tool_schemas):
        if any(m.get("role") == "tool" for m in messages):
            return {"action": "report", "content": "已改配置"}
        return {"action": "tool", "name": "update_setting", "arguments": {"key": "daily_apply_limit", "value": "25"}}

    svc = AgentService(engine=eng, make_planner=lambda ui: _planner)

    async def _chat():
        return await svc.chat("把每日上限改成25", "t-auto-set", "autonomous")

    result = asyncio.run(_chat())
    assert result["report"] == "已改配置"
    assert result["status"] == "completed"
    with eng.connect() as conn:
        # autonomous 白名单键直写，不留审批行
        assert conn.exec_driver_sql("SELECT value FROM settings WHERE key='daily_apply_limit'").scalar() == "25"
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM approvals").scalar() == 0


def test_autonomous_sensitive_update_rejected_and_transcript_masked():
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")

    def _planner(messages, tool_schemas):
        if any(m.get("role") == "tool" for m in messages):
            return {"action": "report", "content": "敏感键改不了"}
        return {"action": "tool", "name": "update_setting", "arguments": {"key": "ai_api_key", "value": "sk-SUPER-SECRET"}}

    svc = AgentService(engine=eng, make_planner=lambda ui: _planner)

    async def _chat():
        return await svc.chat("帮我换一下api key", "t-sens", "autonomous")

    result = asyncio.run(_chat())
    assert result["report"] == "敏感键改不了"

    with eng.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM settings WHERE key='ai_api_key'").scalar() == 0  # 拒绝后不落库

    # transcript 脱敏：agent_steps 全量序列化不含原始密钥，但含掩码占位
    with eng.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT kind, tool_name, tool_input, tool_output, llm_decision FROM agent_steps"
        ).fetchall()
    blobs = json.dumps([dict(r._mapping) for r in rows], ensure_ascii=False)
    assert "sk-SUPER-SECRET" not in blobs
    assert "***" in blobs


def test_audit_update_setting_interrupt_then_approve_writes(tmp_path):
    """写工具 + audit → interrupt 挂起；approve 恢复后写入，审批行 approved（§4.3）。"""
    ckpt = tmp_path / "ckpt.db"
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "15")

    def _planner(messages, tool_schemas):
        if any(m.get("role") == "tool" for m in messages):
            return {"action": "report", "content": "已改"}
        return {"action": "tool", "name": "update_setting", "arguments": {"key": "daily_apply_limit", "value": "30"}}

    reg = service.default_registry(eng)
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        app = build_agent_graph(planner=_planner, registry=reg, engine=eng, checkpointer=saver)
        out = app.invoke(
            {"thread_id": "t-ap-set", "user_input": "改上限", "execution_mode": "audit"},
            config={"thread_id": "t-ap-set", "recursion_limit": DEFAULT_RECURSION_LIMIT},
        )
    assert "__interrupt__" in out  # 写工具 + audit → 挂起等确认
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SELECT value FROM settings WHERE key='daily_apply_limit'").scalar() == "15"  # 未执行
        ap = conn.exec_driver_sql("SELECT status FROM approvals").fetchone()
        assert ap and ap[0] == state.ApprovalStatus.PENDING

    with SqliteSaver.from_conn_string(str(ckpt)) as saver2:
        app2 = build_agent_graph(planner=_planner, registry=reg, engine=eng, checkpointer=saver2)
        out2 = app2.invoke(Command(resume="approve"), config={"thread_id": "t-ap-set", "recursion_limit": DEFAULT_RECURSION_LIMIT})
    assert out2["report"] == "已改"
    with eng.connect() as conn:
        assert conn.exec_driver_sql("SELECT value FROM settings WHERE key='daily_apply_limit'").scalar() == "30"
        assert conn.exec_driver_sql("SELECT status FROM approvals").scalar() == state.ApprovalStatus.APPROVED


# ══════════════════════════════════════════════════════════
#  验收 10（Step 3.3）：FlowLock 互斥 + search_jobs / get_conversations_summary
# ══════════════════════════════════════════════════════════


def test_flowlock_semantics_blocking_queue_and_idempotent_release():
    """FlowLock 基本语义：owner 标签 / 非阻塞抢锁失败 / 阻塞排队 / 幂等 release（§4.6）。"""
    lock = FlowLock()
    assert lock.locked() is False
    assert lock.acquire("agent:search", blocking=False) is True
    assert lock.locked() is True
    assert lock.owner == "agent:search"
    # 非阻塞：被占时立即失败，原有持有者不变
    assert lock.acquire("monitor", blocking=False) is False
    assert lock.owner == "agent:search"

    # 阻塞获取排队：起线程验证，锁释放前不返回
    acquired: dict = {}
    t = threading.Thread(target=lambda: acquired.update(got=lock.acquire("queued", blocking=True)))
    t.start()
    time.sleep(0.1)
    assert acquired == {}, "锁被占时阻塞获取应排队等待，而非立即返回"
    lock.release()
    t.join(timeout=5)
    assert not t.is_alive()
    assert acquired.get("got") is True
    assert lock.owner == "queued"

    lock.release()  # 释放 queued 的持有
    lock.release()  # 幂等：未持有再释放为 no-op，不抛 RuntimeError
    assert lock.locked() is False
    # 单例供 boss_app monitor 循环与工具共享（§4.6），默认未占用
    assert default_flow_lock.locked() is False


def test_search_jobs_queues_when_flowlock_held():
    """§3.3/§4.6 验收：FlowLock 被占时工具排队而非并发（锁释放前绝不执行浏览器搜索）。"""
    eng = _engine()
    lock = FlowLock()
    lock.acquire("sync", blocking=False)  # 模拟监控循环正在占用浏览器

    searches = []
    search_started = threading.Event()
    release_gate = threading.Event()

    class _FakeAuto:
        def search(self, keyword, city_code, **kw):
            searches.append(keyword)
            search_started.set()
            release_gate.wait()  # 模拟搜索耗时，若被并发执行这里能观察到
            return [{"title": "排队岗", "company": "甲公司", "url": "https://z.com/q1", "salary": "20-40K", "city": "上海"}]

    search_jobs = search_jobs_factory(
        eng,
        lock=lock,
        get_automation=lambda: _FakeAuto(),
        pw_runner=lambda fn, *a, **k: fn(*a, **k),
    )

    out: dict = {}
    t = threading.Thread(target=lambda: out.update(result=search_jobs(keyword="python")))
    t.start()
    time.sleep(0.15)  # 给排队线程一点启动时间
    assert searches == [], "FlowLock 被占期间工具应排队等待，不得并发执行浏览器搜索"
    assert search_started.is_set() is False

    lock.release()  # 占用方释放 → 工具排到锁 → 继续执行搜索
    release_gate.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert searches == ["python"]
    assert out["result"]["error"] is None
    assert out["result"]["found"] == 1
    assert out["result"]["added"] == 1
    assert lock.locked() is False  # 工具结束已释放锁


def test_search_jobs_persists_discovered_and_dedup_and_restore():
    """search_jobs 入库 status=discovered；按 URL 去重；被过滤的旧岗恢复 pending（§4.2）。"""
    eng = _engine()
    # 存量：一条 filtered（搜索会再次搜到它 → 恢复 pending）
    _insert_job(eng, title="旧岗位", status=state.JobStatus.FILTERED, city="北京")

    def _search(keyword, city_code, **kw):
        return [
            {"title": "新岗位A", "company": "公司A", "url": "https://z.com/a", "salary": "10-20K", "city": "上海", "hr_active_days": 3},
            {"title": "新岗位B", "company": "公司B", "url": "https://z.com/b", "salary": "20-40K", "city": "上海"},
            {"title": "旧岗位", "company": "旧公司", "url": "https://zhaopin.example.com/旧岗位-filtered-北京", "salary": "5-10K", "city": "北京"},
        ]

    search_jobs = search_jobs_factory(
        eng, get_automation=lambda: _FakeSearch(_search),
        pw_runner=lambda fn, *a, **k: fn(*a, **k),
    )

    out = search_jobs(keyword="python")
    assert out["error"] is None
    assert out["added"] == 2
    assert out["deduped"] == 1
    assert out["restored_from_filtered"] == 1

    with eng.connect() as conn:
        rows = conn.exec_driver_sql("SELECT job_title, status FROM applications ORDER BY id").fetchall()
    statuses = {r[0]: r[1] for r in rows}
    assert statuses["新岗位A"] == state.JobStatus.DISCOVERED
    assert statuses["新岗位B"] == state.JobStatus.DISCOVERED
    assert statuses["旧岗位"] == state.JobStatus.PENDING  # filtered → 恢复库存


class _FakeSearch:
    """search() 返回给定列表的假浏览器（复用 _fake_browser 的翻页私有方法形状）。"""

    def __init__(self, fn):
        self.fn = fn
        self.page = type("P", (), {"goto": staticmethod(lambda *a, **k: None)})()

    def search(self, keyword, city_code, **kw):
        return self.fn(keyword, city_code, **kw)

    def _wait_for_jobs_loaded(self, **kw):
        return 5

    def _scroll_all(self):
        pass

    def _extract_job_cards(self):
        return []


def test_search_jobs_l3_validation_and_browser_not_started():
    """search_jobs L3：缺 keyword / max_pages 越界 → error；浏览器未启动 → error。"""
    eng = _engine()
    search_jobs = search_jobs_factory(
        eng, get_automation=lambda: None, pw_runner=lambda fn, *a, **k: fn(*a, **k)
    )

    assert search_jobs()["error"] == "参数校验失败"  # 缺 keyword
    assert search_jobs(keyword="python", max_pages=0)["error"] == "参数校验失败"
    assert search_jobs(keyword="python", max_pages=4)["error"] == "参数校验失败"
    out = search_jobs(keyword="python")
    assert out["error"] == "浏览器未启动"


def test_search_jobs_max_pages_navigates_additional_pages():
    """max_pages≤3：第 1 页走 automation.search，后续页 goto page=N URL（§4.2）。"""
    eng = _engine()
    calls = []

    class _FakePage:
        def goto(self, url, **kw):
            calls.append(("goto", url))

    class _FakeAuto:
        def __init__(self):
            self.page = _FakePage()

        def search(self, keyword, city_code, **kw):
            calls.append(("search", keyword))
            return [{"title": "P1", "company": "A", "url": "https://z.com/p1", "salary": "5-10K", "city": "上海"}]

        def _wait_for_jobs_loaded(self, **kw):
            return 5

        def _scroll_all(self):
            pass

        def _extract_job_cards(self):
            return [{"title": "P2", "company": "B", "url": "https://z.com/p2", "salary": "8-15K", "city": "上海"}]

    search_jobs = search_jobs_factory(
        eng, get_automation=lambda: _FakeAuto(), pw_runner=lambda fn, *a, **k: fn(*a, **k)
    )
    out = search_jobs(keyword="python", max_pages=2)
    assert out["error"] is None
    assert out["added"] == 2
    assert calls[0] == ("search", "python")
    assert calls[1][0] == "goto"
    assert "page=2" in calls[1][1]


def test_get_conversations_summary_reads_mirror_db_masked():
    """get_conversations_summary：本地镜像库概览，手机号脱敏后才出工具（§4.2/§4.3）。"""
    eng = _engine()
    with eng.begin() as conn:
        conn.execute(
            models.Conversation.__table__.insert().values(
                hr_name="张伟", hr_company="甲公司", job_title="后端工程师",
                last_message_text="联系我 13800138000", last_message_from="hr",
                unread_count=2, online_status="在线",
            )
        )
        conn.execute(
            models.Conversation.__table__.insert().values(
                hr_name="李娜", hr_company="乙公司", job_title="前端工程师",
                last_message_text="明天面试", last_message_from="hr",
                unread_count=0, online_status="离线",
            )
        )

    summary = get_conversations_summary_factory(eng)
    out = summary()
    assert out["error"] is None
    assert out["total"] == 2
    assert out["unread_total"] == 1
    assert len(out["conversations"]) == 2

    texts = [c["last_message_text"] for c in out["conversations"]]
    assert any("13800138000" in (t or "") for t in texts) is False  # 原手机号不出工具
    assert any("138****8000" in (t or "") for t in texts)            # 掩码后格式

    out2 = summary(only_unread=True)
    assert out2["total"] == 1
    assert out2["conversations"][0]["hr_name"] == "张伟"

    out3 = summary(hr_name="李娜")
    assert out3["total"] == 1
    assert out3["conversations"][0]["hr_name"] == "李娜"

    out4 = summary(limit=51)
    assert out4["error"] == "参数校验失败"


def test_default_registry_includes_browser_tools():
    """default_registry 含 search_jobs / get_conversations_summary，write=False（§4.2 读浏览器分类）。"""
    eng = _engine()
    reg = service.default_registry(eng)
    for name in ("search_jobs", "get_conversations_summary"):
        tool = reg.get(name)
        assert tool is not None, f"{name} 应在默认注册表"
        assert tool.write is False, f"{name} 是读工具，audit 直放不留审批"


def test_agent_chat_search_jobs_end_to_end():
    """AgentService 全链路：planner 调 search_jobs → 入库 discovered → 汇报（autonomous 直放）。"""
    eng = _engine()

    def _planner(messages, tool_schemas):
        if any(m.get("role") == "tool" for m in messages):
            return {"action": "report", "content": "已搜新岗位"}
        return {"action": "tool", "name": "search_jobs", "arguments": {"keyword": "python"}}

    def _search(keyword, city_code, **kw):
        return [{"title": "AI工程师", "company": "甲公司", "url": "https://z.com/x1", "salary": "20-40K", "city": "上海"}]

    reg = service.default_registry(
        eng,
        get_automation=lambda: _FakeSearch(_search),
        pw_runner=lambda fn, *a, **k: fn(*a, **k),
    )
    svc = AgentService(engine=eng, make_planner=lambda ui: _planner, registry=reg)

    async def _chat():
        return await svc.chat("帮我搜一下python岗位", "t-search", "autonomous")

    result = asyncio.run(_chat())
    assert result["report"] == "已搜新岗位"
    assert result["status"] == "completed"
    with eng.connect() as conn:
        statuses = [r[0] for r in conn.exec_driver_sql("SELECT status FROM applications")]
        assert statuses == [state.JobStatus.DISCOVERED]
