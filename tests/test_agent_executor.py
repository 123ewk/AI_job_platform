"""SDD Step 4.1：后台任务执行器骨架验收（红→绿，先红）。

本文件先存在（红，`agent/executor.py` 尚未实现），实现后绿。覆盖 §4.5 长任务后台执行
骨架 + spec §7 line 263 验收点——agent_tasks 状态机 + asyncio 执行器，用假长任务
（sleep 循环）测 **进度事件** 与 **stop**：

1. **状态机驱动**：执行器把 agent_tasks 从 `pending → running → 终态`，
   `completed`/`failed`/`stopped` 都走 `state.can_transition` 合法路径（§4.5）。
2. **进度事件**：每完成一个单位，"完成一个岗位" → `progress_done` 加一 +
   broadcast 一次 `agent_task_progress`（WS 通道）；对话里 Agent 据此能答"还剩几个"。
3. **用户手动 stop（配料验收焦点）**：`submit_stop` 给任务打停止标志，执行器在
   **单位与单位之间** 检查（绝不打断正在跑的单位），当前单位完整结束后任务进入
   `stopped` 终态并广播 `agent_task_done`——后续单位不再跑。
4. **失败**：单位函数抛异常 → 任务 `failed`，error 落库，广播终态。
5. **同步单位函数兼容**：unit_fn 为同步函数时经 `asyncio.to_thread` 在线程池跑
   （与 pw 单线程池思想一致，不阻塞事件循环）。

mock/隔离：所有用例用内存 SQLite + StaticPool（与 test_agent_tools 同一套夹具），
executor 注入该引擎；broadcast 收进列表断言事件流；不碰真实库、不启动 Playwright。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import state
from agent.executor import TaskExecutor
from db import models


def _engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


def _task_row(eng, task_id) -> models.AgentTask:
    with SASession(eng) as s:
        return s.get(models.AgentTask, task_id)


def _terminal(events: list[dict]) -> dict | None:
    for e in events:
        if e.get("type") == "agent_task_done":
            return e
    return None


# ══════════════════════════════════════════════════════════
#  1. 状态机驱动 completed + 进度事件
# ══════════════════════════════════════════════════════════


def test_executor_completed_with_progress_and_terminal():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _fake_unit(i):
        await asyncio.sleep(0.01)
        return i

    async def scenario():
        task_id = ex.submit(kind="sync_fake", total=5, unit_fn=_fake_unit)
        await ex._tasks[task_id]
        result["task_id"] = task_id
        result["events"] = list(ws)
        result["row"] = _task_row(eng, task_id)

    asyncio.run(scenario())

    row = result["row"]
    assert row.status == state.TaskStatus.COMPLETED
    assert row.progress_done == 5
    assert row.progress_total == 5
    assert row.error is None
    assert row.started_at is not None and row.finished_at is not None
    assert row.started_at <= row.finished_at

    # 进度事件按单位 1..5 递增广播
    progress = [e for e in result["events"] if e.get("type") == "agent_task_progress"]
    assert [e["done"] for e in progress] == [1, 2, 3, 4, 5]
    assert all(e["total"] == 5 for e in progress)
    assert all(e["task_id"] == result["task_id"] for e in progress)

    # 终态广播
    t = _terminal(result["events"])
    assert t is not None
    assert t["status"] == state.TaskStatus.COMPLETED
    assert t["done"] == 5 and t["total"] == 5
    assert t["error"] is None


# ══════════════════════════════════════════════════════════
#  2. 用户手动 stop：当前单位发完才停，后续不再跑（配料验收焦点）
# ══════════════════════════════════════════════════════════


def test_executor_stop_after_current_unit():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def scenario():
        completed: list[int] = []
        gate = asyncio.Event()  # 挂起单位 3，让"正在执行"成为确定的观测点

        async def _fake_unit(i):
            if i == 3:
                await gate.wait()  # 单位 3 被挂起，直到测试放行 —— 它在跑、但没完
            completed.append(i)
            await asyncio.sleep(0.005)

        task_id = ex.submit(kind="sync_fake", total=5, unit_fn=_fake_unit)
        # 等进度 done==2（单位 1、2 已完）→ 此时单位 3 正被 gate 悬起待完
        for _ in range(200):
            row = _task_row(eng, task_id)
            if row.progress_done == 2:
                break
            await asyncio.sleep(0.005)
        # 在单位 3 执行期间刹车 —— 它必须完整结束后才允许停
        assert ex.submit_stop(task_id) is True
        gate.set()  # 放行单位 3（"当前岗位发完"）
        await ex._tasks[task_id]
        result["task_id"] = task_id
        result["completed"] = list(completed)
        result["events"] = list(ws)
        result["row"] = _task_row(eng, task_id)

    asyncio.run(scenario())

    row = result["row"]
    # 终态 stopped，progress_done == 已完成单位数（3），不是 total
    assert row.status == state.TaskStatus.STOPPED
    assert row.progress_done == 3
    assert row.finished_at is not None

    # 单位 3 完整完成，单位 4/5 不再跑
    assert result["completed"] == [1, 2, 3]

    # 终态广播 stopped
    t = _terminal(result["events"])
    assert t is not None
    assert t["status"] == state.TaskStatus.STOPPED
    assert t["done"] == 3 and t["total"] == 5

    # 后续单位不再发进度（最后一条 dispatch 停在 done=3）
    progress = [e for e in result["events"] if e.get("type") == "agent_task_progress"]
    assert [e["done"] for e in progress] == [1, 2, 3]


# ══════════════════════════════════════════════════════════
#  3. 单位抛异常 → failed + error 落库
# ══════════════════════════════════════════════════════════


def test_executor_failed_on_unit_exception():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _boom_unit(i):
        if i == 2:
            raise RuntimeError("模拟浏览器发送失败")

    async def scenario():
        task_id = ex.submit(kind="sync_fake", total=5, unit_fn=_boom_unit)
        await ex._tasks[task_id]
        result["task_id"] = task_id
        result["events"] = list(ws)
        result["row"] = _task_row(eng, task_id)

    asyncio.run(scenario())

    row = result["row"]
    assert row.status == state.TaskStatus.FAILED
    assert row.error == "模拟浏览器发送失败"
    # 第 1 个单位完成，第 2 个炸了 → progress_done 停在 1
    assert row.progress_done == 1
    assert row.finished_at is not None

    t = _terminal(result["events"])
    assert t is not None
    assert t["status"] == state.TaskStatus.FAILED
    assert t["error"] == "模拟浏览器发送失败"


# ══════════════════════════════════════════════════════════
#  4. 同步单位函数经 to_thread 在线程池跑
# ══════════════════════════════════════════════════════════


def test_executor_sync_unit_ran_via_to_thread():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    def _sync_unit(i):
        # 同步函数：应在线程池执行（当前没在 to_thread 里不会有差，但状态机要正常走）
        return i

    async def scenario():
        task_id = ex.submit(kind="sync_fake", total=3, unit_fn=_sync_unit)
        await ex._tasks[task_id]
        result["task_id"] = task_id
        result["row"] = _task_row(eng, task_id)

    asyncio.run(scenario())

    row = result["row"]
    assert row.status == state.TaskStatus.COMPLETED
    assert row.progress_done == 3


# ══════════════════════════════════════════════════════════
#  5. submit_stop 边界：已知任务 True / 未知任务 False
# ══════════════════════════════════════════════════════════


def test_executor_submit_stop_boundary():
    eng = _engine()
    ex = TaskExecutor(engine=eng)

    async def scenario():
        task_id = ex.submit(kind="sync_fake", total=1, unit_fn=lambda i: None)
        got_known = ex.submit_stop(task_id)
        got_unknown = ex.submit_stop(999999)
        await ex._tasks[task_id]
        return got_known, got_unknown

    got_known, got_unknown = asyncio.run(scenario())
    assert got_known is True
    assert got_unknown is False
