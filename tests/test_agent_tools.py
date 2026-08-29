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

mock/隔离：所有用例用内存 SQLite + StaticPool（`asyncio.to_thread` 跨线程 invoke 需
共享同一连接），插真实 `applications` / `settings` 行，工具走注入引擎、不碰真实库；
审批中断测试用 SqliteSaver 临时文件 + `Command(resume=...)`（进程重启恢复模式）。
"""

from __future__ import annotations

import asyncio
import json

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import create_engine, func
from sqlalchemy.pool import StaticPool

from agent import service, state
from agent.graph import DEFAULT_RECURSION_LIMIT, build_agent_graph
from agent.service import AgentService
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
