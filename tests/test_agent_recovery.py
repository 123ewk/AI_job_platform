"""SDD Step 4.3：崩溃恢复验收（红→绿，先红）。

覆盖 spec §7 line 265 验收点 + DoD §8.3（拔电源重启：任务标 interrupted，无重复发送，
续投需明确确认）：
>

1. **启动时 running→interrupted**：恢复扫描 `agent_tasks` 非终态（pending/running）→
   标 `interrupted` + finished_at。「模拟进程中断」= **直接构造崩溃落盘态**——真实 kill/断电
   （os._exit、段错误）不会执行 `_run` 的 finally，DB 留下 status=running、progress_done=已完成
   单位数的行；进程内无法用"活着被取消"造出该态（finally 总会写终态），故按工件模拟。
2. **"结果未知"岗位隔离**：running 任务被中断时，在途岗位 = `jobs[progress_done]`（已完成
   单位数即下一位 0 基下标，进度在单位完成后才落）→ 置 `applications.status='unknown'`
   （新 JobStatus 值，复用既有 status 列、无迁移）——天然不在 `GREETABLE={pending,discovered}`，
   query_jobs(ungreeted)/send_greetings 库存自动排除 → **无重复发送**。已完成单位
   （≤progress_done）已写库安全、未开始单位安全可续投。
3. **Agent 可提议续投 pending**：安全 pending/discovered 仍在 GREETABLE，恢复后
   query_jobs(ungreeted) 照常放出、send_greetings 可续投；仅 unknown 隔离。
4. **人工确认门** `resolve_unknown_result`：sent_confirm=True → 置 greeted（打招呼确实发出）；
   False → 回 pending（未发出，安全可重发）；非 unknown 岗位拒绝（幂等门）。
5. **接线**：`TaskExecutor.recover()` 方法 + `agent/api.py _get_executor` 建执行器时调一次
   （每进程一次）；`send_greetings` params 记 `job_urls` 供恢复把 progress 下标映射回岗位。

mock/隔离：与 test_agent_tools/send_greetings 同套夹具（内存 SQLite + StaticPool）；恢复与
resolve 只碰 DB；query_jobs/get_progress/send_greetings 全注入假件，不启动 Playwright、不碰
真实库。"""  # noqa: E501

from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import state
from agent.executor import TaskExecutor
from agent.flow_lock import FlowLock
from agent.recovery import recover_interrupted_tasks, resolve_unknown_result
from agent.tools import get_progress_factory, query_jobs_factory, send_greetings_factory
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


def _insert(eng, title, status, *, job_url=None):
    with eng.begin() as conn:
        conn.execute(
            models.Application.__table__.insert().values(
                job_title=title,
                company=f"{title}公司",
                job_url=job_url or f"https://zhipin.example.com/{title}-{status}",
                city="上海",
                status=status,
                description=f"{title} 岗位描述",
            )
        )


def _app_url(eng, title) -> str:
    with SASession(eng) as s:
        return s.execute(
            select(models.Application.job_url).where(models.Application.job_title == title)
        ).scalar_one()


def _app_status(eng, title) -> str:
    with SASession(eng) as s:
        return s.execute(
            select(models.Application.status).where(models.Application.job_title == title)
        ).scalar_one()


def _insert_crashed_task(eng, *, progress_done, progress_total, job_urls, status="running"):
    with eng.begin() as conn:
        r = conn.execute(
            models.AgentTask.__table__.insert().values(
                kind="send_greetings",
                params={"count": len(job_urls), "job_urls": job_urls},
                status=status,
                progress_done=progress_done,
                progress_total=progress_total,
            )
        )
        return int(r.inserted_primary_key[0])


# ══════════════════════════════════════════════════════════
#  1. 启动恢复：running→interrupted + 在途岗位置 unknown
# ══════════════════════════════════════════════════════════


def test_recover_marks_running_interrupted_and_flags_inflight_unknown():
    eng = _engine()
    _insert(eng, "已发完", state.JobStatus.GREETED, job_url="https://zhipin.example.com/a1")
    _insert(eng, "在途", state.JobStatus.PENDING, job_url="https://zhipin.example.com/a2")
    _insert(eng, "未开始", state.JobStatus.PENDING, job_url="https://zhipin.example.com/a3")
    urls = [
        "https://zhipin.example.com/a1",
        "https://zhipin.example.com/a2",
        "https://zhipin.example.com/a3",
    ]
    # 崩溃落盘态：单位1完成（greeted）、单位2在途（apply_batch 已发但 _mark_greeted 未落库）、单位3未开始
    _insert_crashed_task(eng, progress_done=1, progress_total=3, job_urls=urls, status="running")

    report = recover_interrupted_tasks(eng)

    assert report["interrupted"] == 1
    # 任务 → interrupted（终态）
    with SASession(eng) as s:
        row = s.execute(select(models.AgentTask)).scalar_one()
    assert row.status == state.TaskStatus.INTERRUPTED
    assert row.finished_at is not None

    # 在途单位2（index=progress_done=1 → urls[1]=a2）置 unknown；已完成(已greeted)/未开始不变
    assert _app_status(eng, "在途") == state.JobStatus.UNKNOWN
    assert _app_status(eng, "已发完") == state.JobStatus.GREETED
    assert _app_status(eng, "未开始") == state.JobStatus.PENDING
    assert [u["job_url"] for u in report["unknown_jobs"]] == ["https://zhipin.example.com/a2"]


# ══════════════════════════════════════════════════════════
#  2. pending 任务（_run 从未启动）无在途岗位 → 不隔离任何岗位
# ══════════════════════════════════════════════════════════


def test_recover_pending_task_never_started_no_unknown():
    eng = _engine()
    _insert(eng, "岗位A", state.JobStatus.PENDING, job_url="https://zhipin.example.com/pa")
    _insert_crashed_task(
        eng, progress_done=0, progress_total=1,
        job_urls=["https://zhipin.example.com/pa"], status="pending",
    )
    report = recover_interrupted_tasks(eng)
    assert report["interrupted"] == 1
    assert report["unknown_jobs"] == []
    # 任务标 interrupted，但岗位未被触碰（pending 从未 _run）
    assert _app_status(eng, "岗位A") == state.JobStatus.PENDING


# ══════════════════════════════════════════════════════════
#  3. running 但进度已走满（崩溃于最后一次落库后、写终态前）→ 无在途
# ══════════════════════════════════════════════════════════


def test_recover_running_all_done_has_no_inflight():
    eng = _engine()
    _insert(eng, "完成1", state.JobStatus.GREETED, job_url="https://zhipin.example.com/f1")
    _insert(eng, "完成2", state.JobStatus.GREETED, job_url="https://zhipin.example.com/f2")
    _insert_crashed_task(
        eng, progress_done=2, progress_total=2,
        job_urls=["https://zhipin.example.com/f1", "https://zhipin.example.com/f2"],
        status="running",
    )
    report = recover_interrupted_tasks(eng)
    assert report["interrupted"] == 1
    assert report["unknown_jobs"] == []


# ══════════════════════════════════════════════════════════
#  4. 幂等：重复恢复 0 新动作；正常终态任务不受影响
# ══════════════════════════════════════════════════════════


def test_recover_idempotent_and_leaves_terminal_alone():
    eng = _engine()
    _insert(eng, "在途", state.JobStatus.PENDING, job_url="https://zhipin.example.com/i2")
    urls = ["https://zhipin.example.com/i1", "https://zhipin.example.com/i2"]
    _insert_crashed_task(eng, progress_done=1, progress_total=2, job_urls=urls, status="running")
    # 一条已 completed 的任务（正常跑完，恢复不动它）
    with eng.begin() as conn:
        conn.execute(
            models.AgentTask.__table__.insert().values(
                kind="send_greetings", status=state.TaskStatus.COMPLETED,
                params={"job_urls": urls}, progress_done=2, progress_total=2,
            )
        )

    report1 = recover_interrupted_tasks(eng)
    assert report1["interrupted"] == 1
    report2 = recover_interrupted_tasks(eng)
    assert report2["interrupted"] == 0  # 第二次全部终态，无新增
    # 已完成任务不被误标
    with SASession(eng) as s:
        n = s.execute(
            select(func.count()).select_from(models.AgentTask).where(
                models.AgentTask.status == state.TaskStatus.COMPLETED
            )
        ).scalar_one()
    assert n == 1
    # 在途岗位已被隔离为 unknown，第二次恢复不会重复标记
    assert _app_status(eng, "在途") == state.JobStatus.UNKNOWN


# ══════════════════════════════════════════════════════════
#  5. unknown 不出库存：ungreeted 过滤 / get_progress 计数均排除；安全 pending 可续投
# ══════════════════════════════════════════════════════════


def test_unknown_excluded_from_ungreeted_but_safe_pending_continuable():
    eng = _engine()
    _insert(eng, "结果未知", state.JobStatus.UNKNOWN, job_url="https://zhipin.example.com/u")
    _insert(eng, "安全待投", state.JobStatus.PENDING, job_url="https://zhipin.example.com/safe")

    q = query_jobs_factory(eng)(ungreeted=True)
    out_titles = [j["job_title"] for j in q["jobs"]]
    assert "安全待投" in out_titles
    assert "结果未知" not in out_titles  # 无重复发送：unknown 不被当作可打招呼库存

    prog = get_progress_factory(eng)()
    assert prog["ungreeted_count"] == 1  # 只计安全 pending，unknown 排除


# ══════════════════════════════════════════════════════════
#  6. 人工确认门：sent=True → greeted；False → 回 pending
# ══════════════════════════════════════════════════════════


def test_resolve_unknown_sent_true_marks_greeted():
    eng = _engine()
    _insert(eng, "结果未知", state.JobStatus.UNKNOWN, job_url="https://zhipin.example.com/rs")
    aid = _app_id(eng, "结果未知")

    out = resolve_unknown_result(eng, application_id=aid, sent_confirm=True, greeting="您好")

    assert out["status"] == state.JobStatus.GREETED
    with SASession(eng) as s:
        app = s.get(models.Application, aid)
        assert app.status == state.JobStatus.GREETED
        assert app.greeting_text == "您好"
        assert app.greeting_sent_at is not None


def test_resolve_unknown_not_sent_back_to_pending():
    eng = _engine()
    _insert(eng, "结果未知", state.JobStatus.UNKNOWN, job_url="https://zhipin.example.com/rn")
    aid = _app_id(eng, "结果未知")

    out = resolve_unknown_result(eng, application_id=aid, sent_confirm=False)

    assert out["status"] == state.JobStatus.PENDING
    # 回 pending → 重新出现在 ungreeted 库存（可安全重发）
    titles = [j["job_title"] for j in query_jobs_factory(eng)(ungreeted=True)["jobs"]]
    assert "结果未知" in titles


def test_resolve_unknown_rejects_non_unknown_job():
    eng = _engine()
    _insert(eng, "正常岗位", state.JobStatus.GREETED, job_url="https://zhipin.example.com/nz")
    aid = _app_id(eng, "正常岗位")

    out = resolve_unknown_result(eng, application_id=aid, sent_confirm=True)
    assert out["error"] == "非结果未知岗位"


# ══════════════════════════════════════════════════════════
#  7. 接线：send_greetings 记录 job_urls；JobStatus.UNKNOWN 入 ALL 且不在 GREETABLE
# ══════════════════════════════════════════════════════════


def test_job_status_unknown_defined_and_excluded_from_greetable():
    assert state.JobStatus.UNKNOWN in state.JobStatus.ALL
    assert state.JobStatus.UNKNOWN not in state.JobStatus.GREETABLE


def test_send_greetings_params_record_job_urls_for_recovery():
    eng = _engine()
    _insert(eng, "岗位X", state.JobStatus.PENDING, job_url="https://zhipin.example.com/sg1")
    _insert(eng, "岗位Y", state.JobStatus.DISCOVERED, job_url="https://zhipin.example.com/sg2")

    class _Ex:
        def __init__(self):
            self.submits = []

        def submit(self, *, kind, total, unit_fn, params=None, session_id=None):
            self.submits.append(params)
            return 1

    class _Runner:
        def __call__(self, fn, *args, **kwargs):
            return [{"success": True}]

    class _Automation:
        apply_batch = None

    ex = _Ex()
    tool = send_greetings_factory(
        eng, executor=ex,
        lock=FlowLock(), get_automation=lambda: _Automation(), pw_runner=_Runner(),
        paused=lambda: False,
    )
    tool(max_count=5)

    assert len(ex.submits) == 1
    params = ex.submits[0]
    assert params["job_urls"] == [
        "https://zhipin.example.com/sg1",
        "https://zhipin.example.com/sg2",
    ]


# ══════════════════════════════════════════════════════════
#  8. TaskExecutor.recover() 接线方法（启动时恢复入口）
# ══════════════════════════════════════════════════════════


def test_executor_recover_method_flags_unknown():
    eng = _engine()
    _insert(eng, "在途", state.JobStatus.PENDING, job_url="https://zhipin.example.com/er2")
    _insert_crashed_task(eng, progress_done=1, progress_total=2,
                         job_urls=["https://zhipin.example.com/er1", "https://zhipin.example.com/er2"],
                         status="running")

    ex = TaskExecutor(engine=eng)
    report = ex.recover()

    assert report["interrupted"] == 1
    assert _app_status(eng, "在途") == state.JobStatus.UNKNOWN


def _app_id(eng, title) -> int:
    with SASession(eng) as s:
        return int(
            s.execute(
                select(models.Application.id).where(models.Application.job_title == title)
            ).scalar_one()
        )
