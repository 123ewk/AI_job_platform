"""SDD Step 5.3：DRY_RUN 演练验收（红→绿，先红，spec §7 line 272）。

覆盖 §5.3「DRY_RUN 演练」验收点：

1. **全局 dry_run 设置**：手动设置 API `SettingsUpdate` 含 `dry_run` 字段（人工唯一可写路径
   `/api/settings`）+ Agent 白名单含 `dry_run`（对齐测试保绿）。但 dry_run 是**系统级安全
   开关**（新增 `state.SAFETY_SETTING_KEYS`）——`update_setting` 全模式硬拒（Agent 不得
   关掉演练保护，§4.3"系统级安全规则不可被 LLM 覆盖"）。
2. **send_greetings 在 dry_run 下只记"将要发送"不发浏览器**：工具仍提交后台任务（审批门 /
   进度 / 终态全链路照常演练），但每个后台单位**不碰浏览器、不改状态**——只记一条
   "DRY_RUN 演练：将要发送…" 日志 + 返回 would_send 载荷；job 保持 ungreeted
   （演练不消耗真实库存 / 今日额度，可安全重来）。
3. **完整 E2E**：自然语言 → query_jobs 查库存 → send_greetings 后台任务（audit 审批挂起）
   → decide 批准 → 后台任务在 dry_run 下跑完（不发浏览器）→ 汇报收尾。全链断言：
   apply_batch **零调用**、greeted **零变更**、审批 approved、任务 completed。

mock/隔离：内存 SQLite + StaticPool；假 executor（记录 submit）/ 假 runner（记录
apply_batch 调用）/ 假 automation / 独立 FlowLock / paused=False（不碰 boss_app）。
E2E 用真 TaskExecutor（后台线程跑单位）+ 注入 planner + 缺省 SqliteSaver 文件
（chat/decide 跨调用原地恢复，与 5.1 同款）。不启动 Playwright。
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import graph, state
from agent.executor import TaskExecutor
from agent.flow_lock import FlowLock
from agent.recovery import recover_interrupted_tasks
from agent.service import AgentService
from agent.tools import (
    build_greeting_unit,
    build_read_tools,
    build_send_tools,
    get_progress_factory,
    send_greetings_factory,
    update_setting_factory,
)
from db import base as db_base
from db import models

# ══════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════


def _engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


def _insert(eng, title, status, *, company_id=None, hr_active_days=-1):
    with eng.begin() as conn:
        conn.execute(
            models.Application.__table__.insert().values(
                job_title=title,
                company=f"{title}公司",
                company_id=company_id,
                job_url=f"https://zhipin.example.com/{title}-{status}",
                city="上海",
                status=status,
                description=f"{title} 岗位描述",
                hr_active_days=hr_active_days,
                hr_active_label=("3天内活跃" if hr_active_days >= 0 else ""),
            )
        )


def _set_setting(eng, key, value):
    """insert-or-update：可对同一 key 反复改值（dry_run 开关演练）。"""
    with eng.begin() as conn:
        updated = conn.execute(
            update(models.Setting).where(models.Setting.key == key).values(value=value)
        ).rowcount
        if not updated:
            conn.execute(models.Setting.__table__.insert().values(key=key, value=value))


def _greeted_count(eng) -> int:
    with SASession(eng) as s:
        return s.execute(
            select(func.count()).select_from(models.Application).where(
                models.Application.status == state.JobStatus.GREETED
            )
        ).scalar_one()


def _greetable_count(eng) -> int:
    with SASession(eng) as s:
        return s.execute(
            select(func.count()).select_from(models.Application).where(
                models.Application.status.in_(sorted(state.JobStatus.GREETABLE))
            )
        ).scalar_one()


class _FakeExecutor:
    """记录 submit 参数 + 捕获单位函数；不真跑后台。"""

    def __init__(self):
        self.submits = []
        self.captured_unit = None

    def submit(self, *, kind, total, unit_fn, params=None, session_id=None, consecutive_fail_threshold=None):
        self.submits.append({"kind": kind, "total": total, "params": params})
        self.captured_unit = unit_fn
        return 99


class _FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, fn, *args, **kwargs):
        job = (kwargs.get("jobs") or [{}])[0]
        self.calls.append(job)
        return [{"success": True, "application_id": 1, "message": "投递成功"}]


class _Automation:
    apply_batch = None  # runner 忽略 fn；仅需属性存在
    page = object()  # 浏览器预检（graph execute 前）按"page 在=已启动"判定，与生产一致


def _tool(eng, *, executor=None, runner=None, flow=None, paused=False):
    ex = executor if executor is not None else _FakeExecutor()
    r = runner if runner is not None else _FakeRunner()
    return send_greetings_factory(
        eng,
        executor=ex,
        lock=flow if flow is not None else FlowLock(),
        get_automation=lambda: _Automation(),
        pw_runner=r,
        paused=lambda: paused,
    ), ex, r


# ══════════════════════════════════════════════════════════
#  1. 全局 dry_run 设置：手动 API 字段 + 白名单 + 安全开关
# ══════════════════════════════════════════════════════════


def test_dry_run_is_manual_setting_and_safety_switch():
    from boss_app import SettingsUpdate  # noqa: PLC0415 懒加载：boss_app 顶层仅注册路由

    # 人工唯一可写路径：手动设置 API 字段集含 dry_run
    assert "dry_run" in SettingsUpdate.model_fields
    # Agent 白名单含 dry_run（== 手动 API 字段集，对齐测试保持绿）
    assert "dry_run" in state.SETTINGS_WHITELIST
    # 但它是系统级安全开关：单独归类，update_setting 全模式硬拒
    assert state.SAFETY_SETTING_KEYS == frozenset({"dry_run"})
    # 安全开关是白名单子集：白名单通过、再由工具层安全护栏硬拒（与敏感键同构）
    assert state.SAFETY_SETTING_KEYS <= state.SETTINGS_WHITELIST


def test_update_setting_rejects_dry_run_safety_switch():
    eng = _engine()
    u = update_setting_factory(eng)

    out = u(key="dry_run", value="0")

    assert out["error"]
    assert "安全开关" in out["message"]
    with eng.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM settings WHERE key='dry_run'"
        ).scalar() == 0  # 拒绝后不落库：Agent 无法自关演练保护


# ══════════════════════════════════════════════════════════
#  2. get_progress 上报 dry_run 标志（Agent 能汇报当前是演练模式）
# ══════════════════════════════════════════════════════════


def test_get_progress_surfaces_dry_run_flag():
    eng = _engine()
    g = get_progress_factory(eng)

    assert g()["dry_run"] is False  # 缺省关（无该设置）
    _set_setting(eng, "dry_run", "1")
    assert g()["dry_run"] is True
    _set_setting(eng, "dry_run", "true")
    assert g()["dry_run"] is True
    _set_setting(eng, "dry_run", "0")
    assert g()["dry_run"] is False
    _set_setting(eng, "dry_run", "")
    assert g()["dry_run"] is False


# ══════════════════════════════════════════════════════════
#  3. send_greetings 在 dry_run 下只记"将要发送"不发浏览器
# ══════════════════════════════════════════════════════════


def test_send_greetings_dry_run_submits_but_units_never_send():
    eng = _engine()
    _set_setting(eng, "dry_run", "1")
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c1", hr_active_days=1)
    _insert(eng, "后端", state.JobStatus.DISCOVERED, company_id="c2")

    tool, ex, r = _tool(eng)
    out = tool(max_count=10)

    # 工具本体照常提交后台任务（审批门/进度/终态全链路照演练）→ 返回 task_id + dry_run 标志
    assert out["error"] is None
    assert out["dry_run"] is True
    assert out["count"] == 2 and out["task_id"] == 99
    assert len(ex.submits) == 1 and ex.submits[0]["kind"] == "send_greetings"

    # 后台每个单位：只记"将要发送"（would_send 载荷），不碰浏览器、不改状态
    unit = ex.captured_unit
    for i in (1, 2):
        res = unit(i)
        assert res["dry_run"] is True and res["success"] is True
        assert res["would_send"]["job_url"]
        assert res["would_send"]["title"] and res["would_send"]["company"]
    assert r.calls == []  # apply_batch 零调用：未实际发送
    assert _greeted_count(eng) == 0  # 不改状态：greeted 零变更
    assert _greetable_count(eng) == 2  # 库存保持演练前，可安全重来


def test_send_greetings_dry_run_off_sends_normally():
    eng = _engine()  # 缺省 dry_run 关（既有行为回归护栏）
    _insert(eng, "AI研发", state.JobStatus.PENDING)

    tool, ex, r = _tool(eng)
    out = tool(max_count=5)
    assert out["dry_run"] is False

    unit = ex.captured_unit
    unit(1)
    assert len(r.calls) == 1  # apply_batch 照常
    assert _greeted_count(eng) == 1  # 置 greeted（既有行为不变）


def test_build_greeting_unit_dry_run_never_loads_browser():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING)

    def bad_loader():
        raise AssertionError("dry_run 不应加载浏览器对象")

    def bad_runner(*a, **k):
        raise AssertionError("dry_run 不应调用浏览器桥")

    jobs = [
        {
            "job_url": "https://zhipin.example.com/x",
            "title": "AI研发",
            "company": "AI研发公司",
            "company_id": "c1",
            "hr_active_days": -1,
            "hr_active_label": "",
            "description": "岗位描述",
        }
    ]
    unit = build_greeting_unit(
        eng, jobs, "您好，我感兴趣", dry_run=True,
        lock=FlowLock(), get_automation=bad_loader, pw_runner=bad_runner,
    )
    res = unit(1)
    assert res["dry_run"] is True and res["success"] is True
    assert _greeted_count(eng) == 0


# ══════════════════════════════════════════════════════════
#  3.5 崩溃恢复：DRY_RUN 任务无"结果未知"岗位，不做 unknown 隔离
# ══════════════════════════════════════════════════════════


def test_recovery_skips_dry_run_inflight_unknown():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c1")  # 已完成单位（safe）
    _insert(eng, "后端", state.JobStatus.PENDING, company_id="c2")  # 在途单位（dry-run 不隔离）
    with eng.begin() as conn:
        conn.execute(
            models.AgentTask.__table__.insert().values(
                kind="send_greetings",
                params={"dry_run": True, "job_urls": [
                    "https://zhipin.example.com/AI研发-pending",
                    "https://zhipin.example.com/后端-pending",
                ]},
                status=state.TaskStatus.RUNNING,
                progress_done=1,
                progress_total=2,
            )
        )

    report = recover_interrupted_tasks(eng)

    # 任务标 interrupted，但演练任务没有"发送结果未知"岗位 → 零隔离
    assert report["interrupted"] == 1
    assert report["unknown_jobs"] == []
    # 在途 job 保持 GREETABLE（真实运行时仍可安全续投，不是 unknown）
    assert _greetable_count(eng) == 2


# ══════════════════════════════════════════════════════════
#  4. 完整 E2E：自然语言 → 查库存 → 后台任务 → 审批 → 汇报（全程 DRY_RUN）
# ══════════════════════════════════════════════════════════


def _send_task_id(eng, sess_id):
    """从 transcript 取 send_greetings 步骤 tool_output.task_id（后台任务 id）。"""
    with SASession(eng) as s:
        rows = s.execute(
            select(models.AgentStep).where(
                models.AgentStep.session_id == sess_id,
                models.AgentStep.tool_name == "send_greetings",
            )
        ).scalars().all()
    for row in rows:
        out = row.tool_output or {}
        if isinstance(out, dict) and out.get("task_id"):
            return out["task_id"]
    raise AssertionError("transcript 中未找到 send_greetings 的 task_id")


def test_dry_run_full_pipeline_inventory_approval_background_report(tmp_path):
    # 文件库 + WAL + 每线程独立连接：graph 的 to_thread worker、executor 后台线程、
    # 主线程轮询各用自己的连接（StaticPool 共享单连接在真并发下会 "cannot commit
    # transaction - SQL statements in progress"），与生产 db.base.get_engine 同款。
    eng = db_base.get_engine(f"sqlite:///{tmp_path.as_posix()}/dry_run_e2e.db")
    models.Base.metadata.create_all(eng)
    _set_setting(eng, "dry_run", "1")
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c1", hr_active_days=1)
    _insert(eng, "后端", state.JobStatus.DISCOVERED, company_id="c2")
    _insert(eng, "前端", state.JobStatus.PENDING, company_id="c3")

    r = _FakeRunner()
    flow = FlowLock()
    ex = TaskExecutor(engine=eng, broadcast=lambda evt: None)

    reg = graph.ToolRegistry()
    reg = build_read_tools(eng, reg)  # query_jobs + get_progress
    reg = build_send_tools(eng, reg, executor=ex, lock=flow,
                           get_automation=lambda: _Automation(), pw_runner=r, paused=lambda: False)

    def planner(messages, tool_schemas):
        content = "\n".join(m.get("content") or "" for m in messages if m.get("role") == "tool")
        if '"task_id"' in content:  # send_greetings 已提交后台任务 → 汇报收尾
            return {"action": "report", "content": "演练完成：已查库存并提交打招呼（DRY_RUN，未实际发送）"}
        if any(m.get("role") == "tool" for m in messages):  # query_jobs 已查库存 → 提交后台任务
            return {"action": "tool", "name": "send_greetings", "arguments": {"max_count": 3}}
        return {"action": "tool", "name": "query_jobs", "arguments": {"ungreeted": True}}

    svc = AgentService(engine=eng, make_planner=lambda _u: planner, registry=reg)

    # ── 自然语言 → 查库存 → 提交后台任务 → 审计审批挂起 ──
    r1 = asyncio.run(svc.chat(
        "看看还有哪些岗位可以打招呼，演练一次", thread_id="t-dry-e2e", execution_mode="audit",
    ))
    assert r1["status"] == "pending_approval"
    ap = r1["approval_pending"]
    assert ap["tool"] == "send_greetings"
    ap_id = ap["approval_id"]
    sess_id = r1["session_id"]

    # ── 审批批准 → 后台任务提交 → 汇报 ──
    r2 = asyncio.run(svc.decide(ap_id, "approve"))
    assert r2["status"] == "completed"
    assert "演练" in (r2["report"] or "")

    # ── 等后台任务终态（真 TaskExecutor 后台线程跑单位）──
    task_id = _send_task_id(eng, sess_id)
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        with SASession(eng) as s:
            row = s.get(models.AgentTask, task_id)
            status = row.status if row is not None else None
        if status in (state.TaskStatus.COMPLETED, state.TaskStatus.FAILED, state.TaskStatus.STOPPED):
            break
        time.sleep(0.05)
    assert status == state.TaskStatus.COMPLETED

    # ── 全链断言：DRY_RUN 未实际发送 ──
    assert r.calls == []  # apply_batch 零调用（3 个单位全演练）
    assert _greeted_count(eng) == 0  # greeted 零变更
    with SASession(eng) as s:
        ap_row = s.get(models.Approval, ap_id)
        assert ap_row is not None and ap_row.status == state.ApprovalStatus.APPROVED
        task_row = s.get(models.AgentTask, task_id)
        assert task_row is not None and task_row.progress_done == 3
