"""agent/api.py — Agent 对话 API 传输层（SDD Step 2.4，§3 对话入口）。

对外暴露两条通道：
- `POST /api/agent/chat`：同步问答回合（调 AgentService 跑完整决策环，返回 report/ask_user）；
- `WebSocket /ws/agent`：步骤进度推送。图每完成一步（plan/execute/report/ask_user），
  graph 的 `on_step` 回调 → `AgentHub.notify_sync` → 桥接到 asyncio，广播给所有已连接客户端。

**跨线程桥接**（graph 阻塞，service.chat 用 `asyncio.to_thread` 丢到线程池，on_step 在
worker 线程触发）：`AgentHub` 用 `asyncio.Queue`（put_nowait 线程安全）+ 后台 pump 任务，
把 worker 线程的事件安全投递到事件循环内的 WebSocket 发送。缺省 on_step=None 时 graph
不回调（2.3 行为不变）。

service 与 hub 从 `app.state` 解析（缺省惰性建真实引擎服务），测程序可注入内存库 +
自定义 planner/hub 做隔离验收；`boss_app` 仅一行 `include_router(agent_router)` 即可接管。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from agent.executor import TaskExecutor
from agent.service import AgentService
from db import base as db_base

# ══════════════════════════════════════════════════════════
#  请求/响应契约
# ══════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    user_input: str = Field(..., min_length=1, description="用户自然语言问题")
    thread_id: str | None = Field(None, description="会话线程 id，缺省随机生成")
    execution_mode: Literal["audit", "autonomous"] = Field(
        "audit", description="§4.3 执行模式：audit 审计默认 / autonomous 全权"
    )


class ChatResponse(BaseModel):
    thread_id: str
    session_id: int | None = None
    report: str | None = None
    ask_user_question: str | None = None
    status: Literal["completed", "ask_user"]


# ══════════════════════════════════════════════════════════
#  AgentHub：/ws/agent 客户端集合 + 跨线程广播桥
# ══════════════════════════════════════════════════════════


class AgentHub:
    """WebSocket 进度推送枢纽。

    `notify_sync` 可从非事件循环线程调用（graph.on_step 在 `asyncio.to_thread` worker 里
    触发），事件经 `asyncio.Queue`（put_nowait 线程安全）+ 后台 pump 任务广播给所有连接。
    """

    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._events: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def notify_sync(self, event: dict) -> None:
        """线程安全入队：被 graph 的 on_step（worker 线程）调用。"""
        self._events.put_nowait(dict(event))

    async def connect(self, ws: WebSocket) -> None:
        self._conns.add(ws)
        await self._ensure_pump()

    async def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)

    async def _ensure_pump(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        while True:
            evt = await self._events.get()
            dead: list[WebSocket] = []
            for ws in list(self._conns):
                try:
                    await ws.send_text(json.dumps(evt, ensure_ascii=False))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._conns.discard(ws)


# ══════════════════════════════════════════════════════════
#  service / hub 解析（app.state 优先，缺省惰性建真实引擎）
# ══════════════════════════════════════════════════════════


def _get_service(request_or_ws) -> AgentService:
    state = request_or_ws.app.state
    svc = getattr(state, "agent_service", None)
    if svc is None:
        svc = AgentService(engine=db_base.get_engine())
        state.agent_service = svc
    return svc


def _get_hub(request_or_ws) -> AgentHub:
    state = request_or_ws.app.state
    hub = getattr(state, "agent_hub", None)
    if hub is None:
        hub = AgentHub()
        state.agent_hub = hub
    return hub


def _get_executor(request_or_ws) -> TaskExecutor:
    """后台任务执行器解析（app.state 优先，缺省惰性建真实引擎 + hub 广播）。

    进度/终态事件走 AgentHub.notify_sync → /ws/agent 广播（spec §4.5 复用 broadcast_ws
    思路，对话里 Agent 能答"后台任务还剩几个"）。Step 4.2 send_greetings 经此提交任务。
    """
    state = request_or_ws.app.state
    ex = getattr(state, "agent_executor", None)
    if ex is None:
        hub = _get_hub(request_or_ws)
        ex = TaskExecutor(
            engine=db_base.get_engine(),
            broadcast=lambda evt: hub.notify_sync(dict(evt)),
        )
        state.agent_executor = ex
    return ex


# ══════════════════════════════════════════════════════════
#  路由
# ══════════════════════════════════════════════════════════

agent_router = APIRouter()


@agent_router.post("/api/agent/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, http_request: Request) -> ChatResponse:
    svc = _get_service(http_request)
    hub = _get_hub(http_request)
    thread_id = req.thread_id or str(uuid.uuid4())

    def on_step(evt: dict) -> None:
        hub.notify_sync({"type": "agent_step", "thread_id": thread_id, **evt})

    result = await svc.chat(
        req.user_input,
        thread_id,
        req.execution_mode,
        on_step=on_step,
    )
    hub.notify_sync({"type": "agent_chat_done", "thread_id": thread_id, **result})
    return ChatResponse(**result)


@agent_router.websocket("/ws/agent")
async def ws_agent(websocket: WebSocket) -> None:
    hub = _get_hub(websocket)
    await websocket.accept()
    await hub.connect(websocket)
    try:
        await websocket.send_json({"type": "agent_connected"})
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.disconnect(websocket)


__all__ = ["AgentHub", "ChatRequest", "ChatResponse", "agent_router"]
