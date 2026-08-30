"""SDD Step 2.4：对话 API 契约验收（红→绿，先红）。

本文件先存在（红，`agent/api.py` / `agent/service.py` 尚未实现），实现后绿。覆盖 §3
对话入口 + §2.4 的两条通道：

1. **POST /api/agent/chat（同步问答回合）**：注入 echo 假工具 + 确定性 planner
   （骨架阶段不接真 LLM/浏览器，curl 即可冒烟），返回 report + session_id + thread_id；
   回合落库（agent_sessions + agent_steps）。
2. **WebSocket /ws/agent（步骤进度推送）**：图每完成一步（plan/execute/report/ask_user）
   通过 on_step 回调经 AgentHub 广播给所有已连接客户端；回合收尾广播 agent_chat_done。
3. **参数校验**：缺/空 user_input → 422。
4. **ask_user 反问通路**：planner 返回 ask_user → 响应带 ask_user_question、status=ask_user。

mock/隔离策略：router 从 `app.state` 取 `agent_service`/`agent_hub`（测程序注入内存 SQLite
引擎 + 自定义 planner；运行时缺省用真实引擎）。每次测试建独立 FastAPI app + 独立 hub，
不污染模块级单例。WS 全程用 fastapi TestClient（starlette）驱动，无需起真实服务器。
"""

from __future__ import annotations

import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agent import api as agent_api
from agent import state
from agent.service import AgentService
from db import models


def _echo_planner_factory(user_input: str):
    """确定性 planner：先调 echo 假工具，再收尾为 report。"""

    def _plan(messages, tool_schemas):
        if not any(m.get("role") == "tool" for m in messages):
            return {"action": "tool", "name": "echo", "arguments": {"text": user_input}}
        return {"action": "report", "content": f"已回显：{user_input}"}

    return _plan


def _ask_user_planner_factory(user_input: str):
    """planner 首步直接反问，验证 ask_user 通路。"""

    def _plan(messages, tool_schemas):
        return {"action": "ask_user", "question": f"要投几个岗位？（参考：{user_input}）"}

    return _plan


def _app(engine, planner_factory) -> tuple[FastAPI, TestClient]:
    app = FastAPI()
    app.state.agent_service = AgentService(engine=engine, make_planner=planner_factory)
    app.state.agent_hub = agent_api.AgentHub()
    app.include_router(agent_api.agent_router)
    return app, TestClient(app)


def _engine() -> type:
    # StaticPool + check_same_thread=False：service.chat 用 asyncio.to_thread 在工作线程
    # invoke 图，内存库须所有线程共享同一连接（否则 :memory: 各连接独立、表消失）。
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


# ══════════════════════════════════════════════════════════
#  验收 1：POST /api/agent/chat 同步问答回合
# ══════════════════════════════════════════════════════════


def test_chat_returns_report_and_session():
    eng = _engine()
    _, client = _app(eng, _echo_planner_factory)

    resp = client.post(
        "/api/agent/chat",
        json={"user_input": "你好,帮我看看", "thread_id": "t1", "execution_mode": "audit"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["thread_id"] == "t1"
    assert body["report"] == "已回显：你好,帮我看看"
    assert body["status"] == "completed"
    assert isinstance(body["session_id"], int)


def test_chat_persists_transcript():
    eng = _engine()
    with Session(eng) as s:
        pass
    _, client = _app(eng, _echo_planner_factory)

    client.post("/api/agent/chat", json={"user_input": "投一下", "thread_id": "t2"})

    with Session(eng) as s:
        sess = s.execute(
            select(models.AgentSession).where(models.AgentSession.graph_thread_id == "t2")
        ).scalar_one()
        assert sess.final_report == "已回显：投一下"
        assert sess.status == state.SessionStatus.COMPLETED
        step_kinds = [
            r.kind
            for r in s.execute(
                select(models.AgentStep)
                .where(models.AgentStep.session_id == sess.id)
                .order_by(models.AgentStep.id)
            ).scalars()
        ]
    # 回灌续排：plan → execute(echo) → plan → report
    assert step_kinds == ["plan", "execute", "plan", "report"]


def test_chat_rejects_missing_input():
    eng = _engine()
    _, client = _app(eng, _echo_planner_factory)
    resp = client.post("/api/agent/chat", json={"user_input": ""})
    assert resp.status_code == 422


# ══════════════════════════════════════════════════════════
#  验收 4：ask_user 反问通路
# ══════════════════════════════════════════════════════════


def test_chat_ask_user_returns_question():
    eng = _engine()
    _, client = _app(eng, _ask_user_planner_factory)
    resp = client.post("/api/agent/chat", json={"user_input": "帮我投几个"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ask_user"
    assert "要投几个岗位" in (body["ask_user_question"] or "")


# ══════════════════════════════════════════════════════════
#  验收 2：WebSocket /ws/agent 步骤进度推送
# ══════════════════════════════════════════════════════════


def test_ws_agent_streams_step_progress():
    eng = _engine()
    app, client = _app(eng, _echo_planner_factory)

    with client:
        with client.websocket_connect("/ws/agent") as ws:
            first = ws.receive_json()
            assert first["type"] == "agent_connected"

            result: dict = {}

            def _chat():
                result["resp"] = client.post(
                    "/api/agent/chat", json={"user_input": "流式一下", "thread_id": "t-ws"}
                )

            t = threading.Thread(target=_chat)
            t.start()

            kinds: list[str] = []
            done = None
            while True:
                evt = ws.receive_json()
                if evt["type"] == "agent_step":
                    kinds.append(evt["kind"])
                elif evt["type"] == "agent_chat_done":
                    done = evt
                    break
            t.join()

            # 骨架 echo 链路：plan → execute → plan → report 逐步推送
            assert kinds == ["plan", "execute", "plan", "report"]
            assert done["report"] == "已回显：流式一下"
            assert done["status"] == "completed"
            assert result["resp"].status_code == 200


# ══════════════════════════════════════════════════════════
#  V1.2.27：递归上限友好收尾 + chat 端点 JSON 兜底
# ══════════════════════════════════════════════════════════


def test_chat_recursion_limit_returns_graceful_report(monkeypatch):
    """工具轮次打满触发 GraphRecursionError 时不再裸抛（此前变成纯文本 500），
    service 包一层友好收尾 report，前端按正常回合渲染。"""
    import asyncio

    from langgraph.errors import GraphRecursionError

    from agent import service as service_mod

    class _BoomGraph:
        def invoke(self, state, config):
            raise GraphRecursionError("Recursion limit of 30 reached without hitting a stop condition.")

    monkeypatch.setattr(service_mod, "build_agent_graph", lambda **kw: _BoomGraph())
    svc = service_mod.AgentService(engine=_engine(), checkpointer=object())
    out = asyncio.run(svc.chat("帮我获取杭州的大模型应用开发实习生的岗位", "t-recur"))
    assert out["status"] == "completed"
    assert "安全上限" in (out["report"] or "")
    assert out["ask_user_question"] is None
    assert out.get("approval_pending") is None


def test_default_recursion_limit_raised_to_30():
    """V1.2.27：一轮"查库存→搜索→复核"就是 8-10 步（plan+execute 各计一步），
    12 的旧上限会把正常多轮流程误熔断；提到 30 并用断言守住。"""
    from agent.graph import DEFAULT_RECURSION_LIMIT

    assert DEFAULT_RECURSION_LIMIT == 30


def test_chat_unexpected_error_returns_json_not_plain_text(monkeypatch):
    """chat 端点兜底：任何未预期异常也必须回 JSON（report 可渲染），
    绝不让前端收到纯文本 Internal Server Error（r.json() 解析报 Unexpected token）。"""
    app, client = _app(_engine(), _echo_planner_factory)

    async def _boom(*a, **k):
        raise RuntimeError("db on fire")

    monkeypatch.setattr(app.state.agent_service, "chat", _boom)
    resp = client.post("/api/agent/chat", json={"user_input": "hi"})
    assert resp.status_code == 500
    data = resp.json()
    assert "服务器内部错误" in (data["report"] or "")
    assert data["thread_id"]
