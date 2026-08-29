"""SDD Step 4.4：用户手动刹车与熔断验收（红→绿，先红）。

覆盖 spec §7 line 266 验收点 —— dashboard 停止按钮对应的 `POST /api/agent/tasks/{id}/stop`
（取消标志，岗位间检查，当前岗位发完才停，终态 stopped）+ 既有连续失败熔断联动 + WS 广播
任务终态：

1. **API 停止端点**：`POST /api/agent/tasks/{task_id}/stop` 把停止意图中转到执行器
   `submit_stop`（非 Agent 工具，刹车柄只在用户手里 §4.2/§4.5）——已知任务 accepted=True，
   未知/已结束任务 accepted=False；`default_registry` 不注册任何 stop 工具（Agent 无叫停自己
   后台任务的能力）。
2. **连续失败熔断联动**（executor 新增，加参默认 None 保留既有 fail-fast）：
   - `consecutive_fail_threshold=None`（默认）→ 首个单位异常即任务 failed（error=异常原文，
     与 4.1 行为一致，历史测试不破）；
   - `threshold=N` → 容忍 N-1 个**连续**单位异常（每个被吞掉、岗位保持未发仍可重试），
     第 N 个连续失败才熔断：任务 failed + error 带"熔断"，剩余单位不再执行；成功单位清零
     （瞬败不拖累整批）/ 持续崩则停而不空转（浏览器可能卡死）。
3. **stop 接线复核**：executor 层"停止请求后当前岗位发完才停、后续不再发"已在
   tests/test_agent_executor.py 的 stop 验收焦点覆盖（本文件不重复建房），此处只补
   API 契约 + 熔断能力。

mock/隔离：executor 熔断用例用 asyncio.run + 内存 SQLite + StaticPool（同 test_agent_executor
夹具，不碰真实库/浏览器）；API 端点用 fastapi TestClient + 注入假 executor（记录 submit_stop
调用），不启动 Playwright/不真跑后台。
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import api as agent_api
from agent import service, state
from agent.executor import TaskExecutor
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


def _task_row(eng, task_id) -> models.AgentTask:
    with SASession(eng) as s:
        return s.get(models.AgentTask, task_id)


def _terminal(events: list[dict]) -> dict | None:
    for e in events:
        if e.get("type") == "agent_task_done":
            return e
    return None


class _RecorderExecutor:
    """API 端点用的假执行器：记录 submit_stop 调用，已知 task_id 返回 True。"""

    def __init__(self, known: set[int]):
        self.known = known
        self.calls: list[int] = []

    def submit_stop(self, task_id: int) -> bool:
        self.calls.append(task_id)
        return task_id in self.known


def _stop_app(ex: _RecorderExecutor) -> TestClient:
    app = FastAPI()
    app.state.agent_executor = ex
    app.include_router(agent_api.agent_router)
    return TestClient(app)


# ══════════════════════════════════════════════════════════
#  1. 连续失败熔断联动（executor）
# ══════════════════════════════════════════════════════════


def test_circuit_tolerates_transient_failure_then_completes():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _unit(i):
        if i == 1:
            raise RuntimeError("瞬时浏览器失败")

    async def scenario():
        task_id = ex.submit(
            kind="circuit", total=3, unit_fn=_unit, consecutive_fail_threshold=3
        )
        await ex._tasks[task_id]
        result["row"] = _task_row(eng, task_id)
        result["events"] = list(ws)

    asyncio.run(scenario())

    # 单次瞬败被吞（未达阈值），后续成功 → 任务正常 completed
    row = result["row"]
    assert row.status == state.TaskStatus.COMPLETED
    assert row.error is None
    assert row.progress_done == 3
    t = _terminal(result["events"])
    assert t is not None and t["status"] == state.TaskStatus.COMPLETED


def test_circuit_trips_on_consecutive_threshold():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _boom(i):
        raise RuntimeError("浏览器崩了")

    async def scenario():
        task_id = ex.submit(
            kind="circuit", total=4, unit_fn=_boom, consecutive_fail_threshold=2
        )
        await ex._tasks[task_id]
        result["row"] = _task_row(eng, task_id)
        result["events"] = list(ws)

    asyncio.run(scenario())

    # 连续 2 败（=阈值）即熔断：任务 failed、error 带"熔断"、剩余单位不再跑
    row = result["row"]
    assert row.status == state.TaskStatus.FAILED
    assert "熔断" in (row.error or "")
    assert row.progress_done == 0  # 没有成功单位
    t = _terminal(result["events"])
    assert t is not None and t["status"] == state.TaskStatus.FAILED
    assert "熔断" in (t["error"] or "")


def test_circuit_counter_resets_after_success():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _unit(i):
        if i in (1, 3, 4):
            raise RuntimeError("偶发失败")

    async def scenario():
        task_id = ex.submit(
            kind="circuit", total=4, unit_fn=_unit, consecutive_fail_threshold=2
        )
        await ex._tasks[task_id]
        result["row"] = _task_row(eng, task_id)
        result["events"] = list(ws)

    asyncio.run(scenario())

    # 单位1败(1)、单位2成(清零)、单位3败(1)、单位4败(2=阈值) → 熔断；成功点 done=2
    row = result["row"]
    assert row.status == state.TaskStatus.FAILED
    assert "熔断" in (row.error or "")
    assert row.progress_done == 2


def test_default_still_fail_fast_on_first_exception():
    eng = _engine()
    ws: list[dict] = []
    ex = TaskExecutor(engine=eng, broadcast=ws.append)
    result: dict = {}

    async def _boom(i):
        if i == 2:
            raise RuntimeError("模拟浏览器发送失败")

    async def scenario():
        task_id = ex.submit(kind="circuit", total=5, unit_fn=_boom)  # 不传阈值 → 默认 fail-fast
        await ex._tasks[task_id]
        result["row"] = _task_row(eng, task_id)
        result["events"] = list(ws)

    asyncio.run(scenario())

    # 默认 None：首个异常即 failed，error 是异常原文（不含"熔断"措辞）→ 既有 4.1 行为不破
    row = result["row"]
    assert row.status == state.TaskStatus.FAILED
    assert row.error == "模拟浏览器发送失败"
    assert "熔断" not in (row.error or "")
    assert row.progress_done == 1


# ══════════════════════════════════════════════════════════
#  2. API 停止端点契约
# ══════════════════════════════════════════════════════════


def test_stop_endpoint_relays_to_executor_known_task():
    ex = _RecorderExecutor(known={7})
    client = _stop_app(ex)

    r = client.post("/api/agent/tasks/7/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["task_id"] == 7
    assert ex.calls == [7]  # 停止意图已中转给执行器


def test_stop_endpoint_unknown_task_rejected():
    ex = _RecorderExecutor(known=set())
    client = _stop_app(ex)

    r = client.post("/api/agent/tasks/999/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert "结束" in body["message"] or "不存在" in body["message"]
    assert ex.calls == [999]  # 依旧问过一次执行器（提交过一次 stop 判定）


# ══════════════════════════════════════════════════════════
#  3. 停止后台任务不是 Agent 工具（§4.5 刹车柄只在用户手里）
# ══════════════════════════════════════════════════════════


def test_stop_not_registered_as_agent_tool():
    eng = _engine()
    reg = service.default_registry(eng, executor=_RecorderExecutor(known=set()))
    # Agent 命名空间里没有任何"stop/停止"工具——Agent 不能叫停自己后台任务
    assert "stop_agent_task" not in reg.names()
    assert "stop_task" not in reg.names()
    assert "stop" not in reg.names()
