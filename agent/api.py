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
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from agent.executor import TaskExecutor
from agent.service import (
    AgentService,
    ApprovalAlreadyDecidedError,
    ApprovalNotFoundError,
)
from db import base as db_base
from db import models

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
    approval_pending: dict | None = Field(
        None, description="Step 5.1 审计挂起时返回：{tool, arguments, approval_id}，status=pending_approval"
    )
    status: Literal["completed", "ask_user", "pending_approval"]


class DecideApprovalRequest(BaseModel):
    """Step 5.1 审批门 decide：人工对审计挂起的写操作放行 / 拒绝（§4.1/§4.3）。"""

    decision: Literal["approve", "reject"] = Field(..., description="approve=放行执行 / reject=拒绝并回灌 LLM 改道")


class ResolveUnknownRequest(BaseModel):
    """Step 4.3 人工确认门：崩溃恢复隔离的"结果未知"岗位由人决定可不可重发。"""

    sent_confirm: bool = Field(
        ..., description="人工确认打招呼是否确实发出：true=已发→置 greeted；false=未发→回 pending 可安全重发"
    )
    greeting: str | None = Field(None, description="sent_confirm=true 时补招呼语原文（可缺省）")


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
        ex.recover()  # Step 4.3 启动崩溃恢复（每进程一次）
        state.agent_executor = ex
    return ex


def _executor_engine(ex: TaskExecutor):
    """执行器关联的 DB engine（fake 执行器没有 _get_engine 时回退全局 engine）。"""
    getter = getattr(ex, "_get_engine", None)
    return getter() if getter is not None else db_base.get_engine()


# ══════════════════════════════════════════════════════════
#  路由
# ══════════════════════════════════════════════════════════

agent_router = APIRouter()

_log = logging.getLogger(__name__)


@agent_router.post("/api/agent/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, http_request: Request) -> ChatResponse:
    svc = _get_service(http_request)
    hub = _get_hub(http_request)
    thread_id = req.thread_id or str(uuid.uuid4())

    def on_step(evt: dict) -> None:
        hub.notify_sync({"type": "agent_step", "thread_id": thread_id, **evt})

    try:
        result = await svc.chat(
            req.user_input,
            thread_id,
            req.execution_mode,
            on_step=on_step,
        )
    except Exception as e:  # noqa: BLE001 兜底（V1.2.27）：未预期异常也必须回 JSON——
        # 裸 500 的纯文本 "Internal Server Error" 会让前端 r.json() 直接解析报错
        _log.exception("agent chat 未预期异常 thread=%s", thread_id)
        return JSONResponse(
            status_code=500,
            content={
                "thread_id": thread_id,
                "session_id": None,
                "report": f"⚠ 服务器内部错误（已记入后台日志）：{e}",
                "ask_user_question": None,
                "status": "completed",
            },
        )
    hub.notify_sync({"type": "agent_chat_done", "thread_id": thread_id, **result})
    return ChatResponse(**result)


@agent_router.get("/api/agent/tasks")
async def list_agent_tasks(http_request: Request) -> dict:
    """后台任务列表（最近 30 条，id 倒序）——dashboard 面板刷新/重开后的**状态重建**入口。

    V1.2.28 修复配套：任务卡片此前只由 WS `agent_task_progress/done` 事件创建，页面刷新
    即丢（正在跑的任务在 UI 上"消失"，用户既看不到进度也没法停）。前端加载时拉本端点，
    对 pending/running 任务重建卡片（终态任务不重建，历史以 DB 为准）。
    """
    ex = _get_executor(http_request)  # 确保执行器已初始化（崩溃恢复先跑）
    with SASession(_executor_engine(ex)) as s:
        rows = s.execute(
            select(models.AgentTask).order_by(models.AgentTask.id.desc()).limit(30)
        ).scalars().all()
        tasks = [
            {
                "task_id": t.id,
                "kind": t.kind,
                "status": t.status,
                "done": t.progress_done,
                "total": t.progress_total,
                "error": t.error,
                "dry_run": bool((t.params or {}).get("dry_run")),
            }
            for t in rows
        ]
    return {"tasks": tasks}


@agent_router.post("/api/agent/tasks/{task_id}/stop")
async def stop_agent_task(task_id: int, http_request: Request) -> dict:
    """Step 4.4 用户手动刹车：给后台任务打停止标志（§4.5 刹车柄只在用户手里）。

    **不是 Agent 工具**——dashboard 任务卡片的"停止"按钮调用此端点；Agent 对话里不提供
    叫停自己后台任务的能力。执行器在**岗位与岗位之间**检查停止标志，当前岗位完整结束后
    任务进入 `stopped` 终态并广播 `agent_task_done`（WS，spec §4.5）——绝不打断正在
    发送的岗位，避免"发送结果未知"状态。

    返回 `accepted=true`（已请求停止，下一步看 WS 终态）/ `false`（任务不在运行中，
    附当前状态说明——V1.2.28 起拒绝时说明原因，不再让用户猜"点了没反应"）。
    """
    ex = _get_executor(http_request)
    accepted = ex.submit_stop(task_id)
    if accepted:
        message = "已请求停止：当前岗位发完即停（终态 stopped，见 /ws/agent 广播）"
    else:
        with SASession(_executor_engine(ex)) as s:
            row = s.get(models.AgentTask, task_id)
        current = row.status if row is not None else "不存在"
        message = f"任务不在运行中（状态：{current}），无需停止"
    return {"task_id": task_id, "accepted": accepted, "message": message}


@agent_router.post("/api/agent/approvals/{approval_id}/decide", response_model=ChatResponse)
async def decide_approval(approval_id: int, body: DecideApprovalRequest, http_request: Request) -> ChatResponse:
    """Step 5.1 审批门 decide：人工放行 / 拒绝被审计挂起的写操作（§4.1/§4.3）。

    **不是 Agent 工具**——批准/拒绝的权力只在人工手里（decide 恢复的是 `chat` 已
    interrupt() 挂起的图）：`approve` 放行写工具执行；`reject` 拒绝该次工具调用并把
    「用户拒绝」回灌 planner trace，Agent 据此改道或收尾（拒绝 ≠ 终止会话）。原来被
    挂起的会话经 SqliteSaver checkpoint 原地恢复续跑、返回后续回合结果（可能再次
    pending）。未知审批 → 404；已处理审批重复 decide → 409（幂等门）。
    """
    svc = _get_service(http_request)
    hub = _get_hub(http_request)

    def on_step(evt: dict) -> None:
        hub.notify_sync({"type": "agent_step", "approval_id": approval_id, **evt})

    try:
        result = await svc.decide(approval_id, body.decision, on_step=on_step)
    except (ApprovalNotFoundError, ApprovalAlreadyDecidedError) as e:
        from fastapi.responses import JSONResponse

        code = 409 if isinstance(e, ApprovalAlreadyDecidedError) else 404
        return JSONResponse(status_code=code, content={"detail": str(e)})
    hub.notify_sync({"type": "agent_chat_done", **result})
    return ChatResponse(**result)


@agent_router.post("/api/agent/applications/{application_id}/resolve-unknown")
async def resolve_unknown(application_id: int, body: ResolveUnknownRequest, http_request: Request) -> dict:
    """Step 4.3 人工确认门：确认崩溃隔离的"结果未知"岗位（sent 与否）→ 决定可不可重发。

    仅供**人工**调用（dashboard/手动），不是 Agent 工具——Agent 不得自证已发。sent_confirm
    =true 置 greeted（无重复发送），false 回 pending（进库存可安全续投）。
    """
    from agent.recovery import resolve_unknown_result  # noqa: PLC0415

    return resolve_unknown_result(
        db_base.get_engine(),
        application_id=application_id,
        sent_confirm=body.sent_confirm,
        greeting=body.greeting,
    )


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


__all__ = ["AgentHub", "ChatRequest", "ChatResponse", "ResolveUnknownRequest", "DecideApprovalRequest", "agent_router"]
