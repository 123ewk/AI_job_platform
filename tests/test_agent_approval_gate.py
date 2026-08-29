"""SDD Step 5.1：审计审批门 HTTP 层验收（红→绿，先红，spec §7 line 270）。

覆盖 §4.1/§4.3 审批门的完整闭环——audit 下写操作挂起（graph 层 interrupt 已有，
2.3 已验证），本步补齐「人工透过 HTTP/WS 驱动审批」的传输与接线：

1. **写操作挂起 → approvals 落库 → WS 通知**：audit 写工具经 `POST /api/agent/chat`
   触发 graph `interrupt()` 挂起，返回 `status=pending_approval` + `approval_pending`
   （含 approval_id）；`approvals` 表落一条 pending 行；`/ws/agent` 流式收到
   `agent_step` kind=approval 的审批事件（带 approval_id / tool_name）。
2. **decide API 批准**：`POST /api/agent/approvals/{id}/decide {decision:approve}`
   → 恢复挂起的图 → 工具**执行** → 审批行 status=approved + decision=approve、
   终态 completed。
3. **decide API 拒绝 → 结果回灌 → 改道（E2E 验收焦点）**：`decision:reject` →
   工具**不执行**、审批行 rejected，拒绝结果回灌 planner trace，Agent 从被打拒的
   写操作**改道**到只读工具收尾。
4. **decide API 契约**：未知审批 id → 404；已处理审批再 decide → 409。

mock/隔离：与 test_agent_api 同套夹具（内存 SQLite + StaticPool + fastapi
TestClient，跨线程 invoke 共享连接）+ 注入确定性 planner 与 `{echo(只读)，
send_test(写)}` 假工具；checkpointer 用缺省 SqliteSaver 文件（按引擎哈希落到
临时目录，chat/decide 复用同一文件原地恢复，验证跨调用恢复，不碰真实库/浏览器）。
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agent import api as agent_api
from agent import graph, state
from agent.service import AgentService
from db import models

# ══════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════


def _engine():
    # StaticPool + check_same_thread=False：service.chat/decide 用 asyncio.to_thread
    # 在工作线程 invoke 图，内存库须所有线程共享同一连接（否则 :memory: 各连接独立、表消失）。
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


def _echo_schema():
    return {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "回显工具（假只读）",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }


def _send_test_schema():
    return {
        "type": "function",
        "function": {
            "name": "send_test",
            "description": "假写工具（write=True 走审批门）",
            "parameters": {
                "type": "object",
                "properties": {"to": {"type": "string"}},
                "required": ["to"],
            },
        },
    }


def _registry() -> graph.ToolRegistry:
    reg = graph.ToolRegistry()
    reg.register("echo", func=lambda text: {"echo": text, "received": True},
                 description="回显", schema=_echo_schema(), write=False)
    reg.register("send_test", func=lambda to: {"sent": True, "to": to},
                 description="假写", schema=_send_test_schema(), write=True)
    return reg


def _reject_replan_planner_factory(unused_input: str):
    """plan 脚本：第 1 步写工具 send_test（触发审批）→ 被拒后**改道**只读 echo → 收尾。

    依 messages（trace）决策而非调用次数，resume 时按 user_prompt 重新派生同一行为
    planner（service.decide 用），证明拒绝结果**回灌**后 Agent 改走别的方案。
    """

    def _plan(messages, tool_schemas):
        # echo 已改道执行过（输出已回灌）→ 收尾，别被残留的"拒绝"消息再带进 echo 循环
        if any("echo" in (m.get("content") or "") for m in messages if m.get("role") == "tool"):
            return {"action": "report", "content": "已改道收尾"}
        if any("拒绝" in (m.get("content") or "") for m in messages if m.get("role") == "tool"):
            return {"action": "tool", "name": "echo", "arguments": {"text": "已改道"}}
        return {"action": "tool", "name": "send_test", "arguments": {"to": "hr"}}

    return _plan


def _approve_planner_factory(unused_input: str):
    """批准路径 planner：send_test → 执行后 report。"""

    def _plan(messages, tool_schemas):
        if any(m.get("role") == "tool" for m in messages):
            return {"action": "report", "content": "已发送"}
        return {"action": "tool", "name": "send_test", "arguments": {"to": "hr"}}

    return _plan


def _app(eng, planner_factory) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.state.agent_service = AgentService(
        engine=eng, make_planner=planner_factory, registry=_registry()
    )
    app.state.agent_hub = agent_api.AgentHub()
    app.include_router(agent_api.agent_router)
    return app, TestClient(app)


def _steps(engine, sid):
    with Session(engine) as s:
        rows = s.execute(
            select(models.AgentStep).where(models.AgentStep.session_id == sid).order_by(models.AgentStep.id)
        ).scalars()
        return [{"kind": r.kind, "tool_name": r.tool_name, "tool_output": r.tool_output} for r in rows]


def _approval(engine, ap_id) -> models.Approval:
    with Session(engine) as s:
        return s.get(models.Approval, ap_id)


# ══════════════════════════════════════════════════════════
#  1. 写操作挂起：pending_approval 返回 + approvals 落库
# ══════════════════════════════════════════════════════════


def test_chat_write_hangs_pending_approval(tmp_path):
    eng = _engine()
    _, client = _app(eng, _reject_replan_planner_factory)

    r = client.post("/api/agent/chat", json={
        "user_input": "发个招呼", "thread_id": "t-ap-1", "execution_mode": "audit",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending_approval"
    assert body["approval_pending"]["tool"] == "send_test"
    ap_id = body["approval_pending"]["approval_id"]

    ap = _approval(eng, ap_id)
    assert ap is not None and ap.status == state.ApprovalStatus.PENDING
    assert ap.tool_name == "send_test"
    assert ap.decided_at is None


# ══════════════════════════════════════════════════════════
#  2. decide 批准 → 写工具执行
# ══════════════════════════════════════════════════════════


def test_decide_approve_executes_tool(tmp_path):
    eng = _engine()
    _, client = _app(eng, _approve_planner_factory)

    r1 = client.post("/api/agent/chat", json={
        "user_input": "发", "thread_id": "t-ap-2", "execution_mode": "audit",
    })
    ap_id = r1.json()["approval_pending"]["approval_id"]
    sess_id = r1.json()["session_id"]

    r2 = client.post(f"/api/agent/approvals/{ap_id}/decide", json={"decision": "approve"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "completed"
    assert body["report"] == "已发送"

    ap = _approval(eng, ap_id)
    assert ap.status == state.ApprovalStatus.APPROVED
    assert ap.decision == "approve"
    assert ap.decided_at is not None
    # 写工具执行落库
    names = [s2["tool_name"] for s2 in _steps(eng, sess_id) if s2["tool_name"]]
    assert "send_test" in names


# ══════════════════════════════════════════════════════════
#  3. decide 拒绝 → 工具不执行 + 拒绝结果回灌 → Agent 改道（E2E 验收焦点）
# ══════════════════════════════════════════════════════════


def test_decide_reject_replans_changes_tool(tmp_path):
    eng = _engine()
    _, client = _app(eng, _reject_replan_planner_factory)

    r1 = client.post("/api/agent/chat", json={
        "user_input": "发", "thread_id": "t-ap-3", "execution_mode": "audit",
    })
    b1 = r1.json()
    ap_id = b1["approval_pending"]["approval_id"]
    sess_id = b1["session_id"]

    r2 = client.post(f"/api/agent/approvals/{ap_id}/decide", json={"decision": "reject"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"

    ap = _approval(eng, ap_id)
    assert ap.status == state.ApprovalStatus.REJECTED
    assert ap.decision == "reject"

    # 被拒写工具绝不执行；Agent 改为走只读 echo 收尾（拒绝结果已回灌 → 改道）
    names = [s2["tool_name"] for s2 in _steps(eng, sess_id) if s2["tool_name"]]
    assert "send_test" not in names
    assert "echo" in names


# ══════════════════════════════════════════════════════════
#  4. decide API 契约：未知 404 / 已处理 409
# ══════════════════════════════════════════════════════════


def test_decide_unknown_404_and_already_409(tmp_path):
    eng = _engine()
    _, client = _app(eng, _approve_planner_factory)

    # 未知审批 → 404
    r404 = client.post("/api/agent/approvals/99999/decide", json={"decision": "approve"})
    assert r404.status_code == 404

    # 批准后再次 decide → 409（已处理）
    r1 = client.post("/api/agent/chat", json={
        "user_input": "发", "thread_id": "t-ap-4", "execution_mode": "audit",
    })
    ap_id = r1.json()["approval_pending"]["approval_id"]
    assert client.post(f"/api/agent/approvals/{ap_id}/decide", json={"decision": "approve"}).status_code == 200
    r409 = client.post(f"/api/agent/approvals/{ap_id}/decide", json={"decision": "reject"})
    assert r409.status_code == 409


# ══════════════════════════════════════════════════════════
#  5. /ws/agent 审批挂起 WS 通知（approval_id + tool 随事件外发）
# ══════════════════════════════════════════════════════════


def test_ws_approval_pending_event(tmp_path):
    eng = _engine()
    app, client = _app(eng, _reject_replan_planner_factory)

    with client:
        with client.websocket_connect("/ws/agent") as ws:
            assert ws.receive_json()["type"] == "agent_connected"

            def _chat():
                client.post("/api/agent/chat", json={
                    "user_input": "发", "thread_id": "t-ap-5", "execution_mode": "audit",
                })

            t = threading.Thread(target=_chat)
            t.start()

            got: dict | None = None
            while True:
                evt = ws.receive_json()
                if evt["type"] == "agent_step" and evt.get("kind") == "approval":
                    got = evt
                    break
            t.join()

            assert got is not None
            assert got["tool_name"] == "send_test"
            assert isinstance(got["step_id"], int)  # step_id 即审批行 id
            assert _approval(eng, got["step_id"]) is not None
