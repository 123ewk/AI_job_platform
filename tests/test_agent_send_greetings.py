"""SDD Step 4.2：send_greetings 后台打招呼任务验收（红→绿，先红）。

覆盖 §4.2「send_greetings 接入」验收点：

1. **包既有 apply_batch，逐岗位'先写库再发下一个'**：工具本体只查库存 + 提交后台任务
   （**不碰浏览器、不阻塞对话**，浏览器工作在后台单位函数里才发生）；后台每个单位 =
   单岗位 `apply_batch`（`company_id`/`hr_active_days`/`hr_active_label` 全透传——公司去重、
   HR 活跃过滤**沿用 apply_batch 内部逻辑，不重写**），成功后写库置 `greeted` + 招呼语 + 时间戳；
   只有当前岗位已写库（greeted），才发下一个（顺序断言）。
2. **每日上限沿用**：`min(daily_apply_limit, MAX_APPLY_PER_DAY)` 口径（get_progress），
   余量 0 → 拒绝提交；`max_count` 截到剩余额度。
3. **库存**：只选 ungreeted（status∈{pending, discovered}），无库存 → 拒绝提交。
4. **用户暂停（§4.6）**：paused=True → 拒绝，不提交。
5. **L3 校验**：max_count 越界 → error。
6. **真 executor 后台集成**：`build_greeting_unit` 挂到 TaskExecutor 跑完全部单位 → 全部
   greeted + 终态（后台主动发，对话不等待）。

mock/隔离：与 test_agent_tools 同套夹具（内存 SQLite + StaticPool，跨线程 invoke 共享连接）；
注入假 executor（记录 submit 参数）、假 pw_runner（记录 apply_batch 调用、返回成功）、假
automation、独立 FlowLock、paused=lambda: False（不触发 boss_app）。不启动 Playwright。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import service, state
from agent.executor import TaskExecutor
from agent.flow_lock import FlowLock
from agent.tools import build_greeting_unit, send_greetings_factory
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


def _insert(eng, title, status, *, company_id=None, hr_active_days=-1, greeted_today=False):
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
                greeting_sent_at=func.datetime("now", "localtime") if greeted_today else None,
            )
        )


def _set_setting(eng, key, value):
    with eng.begin() as conn:
        conn.execute(models.Setting.__table__.insert().values(key=key, value=value))


def _greeted_count(eng) -> int:
    with SASession(eng) as s:
        return s.execute(
            select(func.count()).select_from(models.Application).where(
                models.Application.status == state.JobStatus.GREETED
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
        self.greetings = []  # V1.2.28：捕获每次 apply_batch 收到的 greeting_template

    def __call__(self, fn, *args, **kwargs):
        job = (kwargs.get("jobs") or [{}])[0]
        self.calls.append(job)
        self.greetings.append(kwargs.get("greeting_template"))
        return [{"success": True, "application_id": 1, "message": "投递成功"}]


class _Automation:
    apply_batch = None  # runner 忽略 fn；仅需属性存在


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
#  1. 提交即返回（不阻塞）+ 后台单位逐岗位"先写库再发下一个"
# ══════════════════════════════════════════════════════════


def test_send_greetings_submits_background_and_greets_sequentially():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c1", hr_active_days=1)
    _insert(eng, "后端", state.JobStatus.DISCOVERED, company_id="c2", hr_active_days=2)
    _insert(eng, "前端", state.JobStatus.PENDING, company_id="c3", hr_active_days=-1)
    _insert(eng, "已打招呼", state.JobStatus.GREETED)  # 非 ungreeted → 不选

    tool, ex, r = _tool(eng)
    out = tool(max_count=10)

    assert out["error"] is None and out["count"] == 3
    # 工具本体不碰浏览器：浏览器调用全部延迟到后台单位，对话不阻塞
    assert r.calls == []

    # 后台提交一次
    assert len(ex.submits) == 1
    sub = ex.submits[0]
    assert sub["kind"] == "send_greetings" and sub["total"] == 3
    assert out["task_id"] == 99

    # 逐岗位执行单位：当前岗位写完库再发下一个（顺序断言）
    unit = ex.captured_unit
    r.calls.clear()
    unit(1)
    assert len(r.calls) == 1
    with SASession(eng) as s:
        j1 = s.execute(
            select(models.Application).where(models.Application.job_title == "AI研发")
        ).scalar_one()
        j2 = s.execute(
            select(models.Application).where(models.Application.job_title == "后端")
        ).scalar_one()
    # 单位 1 完成后只 job1 置 greeted；job2 还没发 → 仍是 discovered（先写库再发下一个）
    assert j1.status == state.JobStatus.GREETED and j1.greeting_sent_at is not None
    assert j2.status == state.JobStatus.DISCOVERED

    unit(2)
    unit(3)
    assert len(r.calls) == 3
    # 三个 ungreeted 目标全部置 greeted（预置的 GREETED 行本就 greeted，不计入）
    with SASession(eng) as s:
        n = s.execute(
            select(func.count()).select_from(models.Application).where(
                models.Application.job_title.in_(["AI研发", "后端", "前端"]),
                models.Application.status == state.JobStatus.GREETED,
            )
        ).scalar_one()
    assert n == 3


# ══════════════════════════════════════════════════════════
#  2. 沿用 apply_batch：去重/HR 活跃字段透传（不重写）
# ══════════════════════════════════════════════════════════


def test_send_greetings_passes_dedup_hr_fields_to_apply_batch():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c-abc", hr_active_days=3)

    tool, ex, r = _tool(eng)
    tool(max_count=5)
    unit = ex.captured_unit
    unit(1)

    job = r.calls[0]
    assert job["url"].endswith("/AI研发-pending")
    assert job["company_id"] == "c-abc"
    assert job["hr_active_days"] == 3
    assert job["hr_active_label"] == "3天内活跃"
    assert job["company"] == "AI研发公司"


# ══════════════════════════════════════════════════════════
#  3. 每日上限沿用 + 库存为空
# ══════════════════════════════════════════════════════════


def test_send_greetings_rejects_when_daily_limit_exhausted():
    eng = _engine()
    _set_setting(eng, "daily_apply_limit", "3")
    for i in range(3):
        _insert(eng, f"已投{i}", state.JobStatus.PENDING, greeted_today=True)
    _insert(eng, "还有库存", state.JobStatus.PENDING)  # 未打招呼库存仍在

    tool, ex, r = _tool(eng)
    out = tool(max_count=10)

    assert out["error"] == "今日额度已用完"
    assert ex.submits == []  # 不提交


def test_send_greetings_rejects_no_ungreeted_inventory():
    eng = _engine()
    _insert(eng, "已打招呼", state.JobStatus.GREETED)
    _insert(eng, "已投递", state.JobStatus.APPLIED)

    tool, ex, r = _tool(eng)
    out = tool(max_count=5)

    assert out["error"] == "没有可打招呼的岗位"
    assert ex.submits == []


# ══════════════════════════════════════════════════════════
#  4. 用户暂停 + L3 校验
# ══════════════════════════════════════════════════════════


def test_send_greetings_respects_paused():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING)

    tool, ex, r = _tool(eng, paused=True)
    out = tool(max_count=5)

    assert out["error"] == "监控已暂停"
    assert ex.submits == []


def test_send_greetings_param_validation():
    eng = _engine()

    tool, ex, r = _tool(eng)
    out = tool(max_count=0)
    assert out["error"] == "参数校验失败"

    out = tool(max_count=51)
    assert out["error"] == "参数校验失败"
    assert ex.submits == []


# ══════════════════════════════════════════════════════════
#  5. 真 executor 后台集成：单位挂后台跑完全部 → 全部 greeted
# ══════════════════════════════════════════════════════════


def test_send_greetings_real_executor_background_greets_all():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING, company_id="c1")
    _insert(eng, "后端", state.JobStatus.DISCOVERED, company_id="c2")
    _insert(eng, "前端", state.JobStatus.PENDING, company_id="c3")

    r = _FakeRunner()
    flow = FlowLock()
    ex = TaskExecutor(engine=eng, broadcast=lambda evt: None)
    job_dicts = [
        {"job_url": f"https://zhipin.example.com/{t}-{s}", "title": t, "company": f"{t}公司",
         "company_id": f"c{i}", "hr_active_days": -1, "hr_active_label": "", "description": f"{t} 岗位描述"}
        for i, (t, s) in enumerate([("AI研发", state.JobStatus.PENDING), ("后端", state.JobStatus.DISCOVERED)], start=1)
    ]
    unit = build_greeting_unit(eng, job_dicts, greeting="您好，我感兴趣", lock=flow,
                               get_automation=lambda: _Automation(), pw_runner=r)

    async def scenario():
        task_id = ex.submit(kind="send_greetings", total=2, unit_fn=unit)
        await ex._tasks[task_id]
        return task_id

    asyncio.run(scenario())

    # 后台跑完全部单位：每个都是 apply_batch 一次 + 落库 greeted
    assert len(r.calls) == 2
    assert _greeted_count(eng) == 2


# ══════════════════════════════════════════════════════════
#  6. 注册：send_greetings write=True，走审批门
# ══════════════════════════════════════════════════════════


def test_send_greetings_registered_write_true():
    eng = _engine()
    _insert(eng, "AI研发", state.JobStatus.PENDING)
    reg = service.default_registry(eng, executor=_FakeExecutor())
    tool = reg.get("send_greetings")
    assert tool is not None and tool.write is True


# ══════════════════════════════════════════════════════════
#  7. 招呼语模板逐岗位实例化（V1.2.28：HR 收到字面 "{job_title}" 回归）
# ══════════════════════════════════════════════════════════


def test_send_greetings_renders_template_placeholders_per_job():
    """settings 的 greeting_template 是带占位符的**模板**：每个岗位发送前按本岗位替换。

    用户实测回归（V1.2.28）：Agent 批量路径此前把原始模板整批直发（_resolve_greeting 只
    resolve 不替换），HR 收到字面 "{job_title}"；手动路径经 generate_greeting 的 .replace()
    所以没事。修复后 Agent 路径与手动路径语义对齐（_render_greeting）。
    """
    eng = _engine()
    _insert(eng, "大模型算法实习生", state.JobStatus.PENDING)
    _insert(eng, "AI应用开发工程师", state.JobStatus.PENDING)
    _set_setting(eng, "greeting_template", "您好！看到贵司在招{job_title}，挺感兴趣的。")

    tool, ex, r = _tool(eng)
    out = tool(max_count=5)
    assert out["error"] is None

    unit = ex.captured_unit
    unit(1)
    unit(2)
    # runner 收到的 greeting_template 已按各自岗位替换（同一模板 → 两句不同招呼语）
    assert r.greetings[0] == "您好！看到贵司在招大模型算法实习生，挺感兴趣的。"
    assert r.greetings[1] == "您好！看到贵司在招AI应用开发工程师，挺感兴趣的。"

    # 写库的招呼语同样是替换后的（dashboard 会话页按 greeting_text 展示）
    with SASession(eng) as s:
        row = s.execute(
            select(models.Application).where(models.Application.job_title == "大模型算法实习生")
        ).scalar_one()
    assert row.greeting_text == "您好！看到贵司在招大模型算法实习生，挺感兴趣的。"
    assert "{job_title}" not in (row.greeting_text or "")


def test_render_greeting_fallback_and_defaults():
    """_render_greeting 边界：字段缺失给默认值；残留未知占位符 → 兜底通用句。"""
    from agent.tools import _render_greeting

    # 正常替换（company 带空白 → strip）
    assert _render_greeting("您好{job_title}@{company}", {"job_title": "AI实习", "company": " ACME "}) == (
        "您好AI实习@ACME"
    )
    # 字段全缺 → 默认值（相关岗位/贵公司），不残留花括号
    assert _render_greeting("您好{job_title}@{company}", {}) == "您好相关岗位@贵公司"
    # 模板写了不支持的变量 → 整句兜底，绝不把 "{xxx}" 发给 HR
    fallback = _render_greeting("您好{job_title}{hr_name}", {"job_title": "AI实习", "company": "ACME"})
    assert "{hr_name}" not in fallback and "AI实习" in fallback


# ══════════════════════════════════════════════════════════
#  8. keyword 关键词筛选库存（V1.3.0：用户反问关键词后定向投递）
# ══════════════════════════════════════════════════════════


def test_send_greetings_keyword_filters_inventory():
    """keyword 参数只投岗位名/公司命中的库存——用户没说关键词 Agent 须反问，说了就定向投。"""
    eng = _engine()
    _insert(eng, "大模型算法实习生", state.JobStatus.PENDING)
    _insert(eng, "前端开发", state.JobStatus.PENDING)
    _insert(eng, "后端开发", state.JobStatus.PENDING)

    tool, ex, r = _tool(eng)
    out = tool(max_count=10, keyword="大模型")

    assert out["error"] is None
    assert out["count"] == 1 and out["keyword"] == "大模型"
    assert ex.submits[0]["params"]["keyword"] == "大模型"  # 任务参数可追溯

    unit = ex.captured_unit
    unit(1)
    with SASession(eng) as s:
        greeted = s.execute(
            select(models.Application.job_title).where(models.Application.status == state.JobStatus.GREETED)
        ).scalars().all()
    assert greeted == ["大模型算法实习生"]  # 只有命中关键词的岗位被动过


def test_send_greetings_keyword_case_insensitive_and_no_match():
    """keyword 大小写不敏感（Java vs java）；无命中时报错且不提交任务。"""
    eng = _engine()
    _insert(eng, "Java后端开发", state.JobStatus.PENDING)
    _insert(eng, "前端开发", state.JobStatus.PENDING)

    tool, ex, r = _tool(eng)
    out = tool(max_count=10, keyword="java")
    assert out["error"] is None and out["count"] == 1

    out2 = tool(max_count=5, keyword="不存在的词")
    assert out2["error"] == "没有匹配关键词的岗位"
    assert "不存在的词" in out2["message"]
    assert ex.submits and len(ex.submits) == 1  # 第二次没有提交（只有第一次的）
