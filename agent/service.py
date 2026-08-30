"""agent/service.py — AgentService：决策图的同步问答编排（SDD Step 2.4，§3 对话入口）。

把 Step 2.3 的 StateGraph 包成可被 HTTP/WS 调用的服务：

- `chat(user_input, thread_id, execution_mode, on_step)` 同步跑完一个问答回合，
  返回 report / ask_user_question / session_id；
- 每完成一步（plan/execute/report/ask_user）经传入的 `on_step` 回调外发——Step 2.4
  对话 API 据此经 WebSocket 推送步骤进度（§3：`/ws/agent` 步骤进度推送）；
- **骨架阶段**：`default_registry()` 只注册 echo 假工具（安全边界 L3，白名单）、
  `echo_planner_factory` 给确定性 planner（不依赖真实 LLM key / 浏览器），curl 即可冒烟；
  Phase 3/4 接真工具时只换注入的 registry + make_planner，服务与路由零改动。

图在 event loop 里是阻塞的，`chat` 用 `asyncio.to_thread` 把 invoke 扔到线程池执行；
`on_step` 在 worker 线程里被调用，调用方（AgentHub）必须做跨线程桥接（队列）到 asyncio。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from typing import Any, Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from agent import state
from agent.graph import DEFAULT_RECURSION_LIMIT, ToolRegistry, build_agent_graph
from agent.planner import default_planner_factory
from db import base as db_base
from db import models


def _runtime_paused() -> bool:
    """运行时用户暂停标志（懒加载，避免 registry 构建期 import boss_app）。

    经 tools.live_boss_app() 取——`python boss_app.py` 启动时 monitor_paused 挂在
    __main__ 上，直接 `from boss_app import monitor_paused` 读到影子副本恒 False。
    """
    from agent.tools import live_boss_app  # noqa: PLC0415

    return bool(getattr(live_boss_app(), "monitor_paused", False))


def default_registry(engine=None, *, lock=None, get_automation=None, pw_runner=None, executor=None) -> ToolRegistry:
    """骨架注册表：echo 假工具（全链路验证）+ 只读 query_jobs/get_progress（3.1）
    + 写配置 update_setting（3.2）+ 浏览器 search_jobs / 会话概览 get_conversations_summary（3.3）
    + 后台打招呼 send_greetings（4.2，write=True）。

    写工具置 write=True 走审批门；search_jobs 是"读浏览器"工具（write=False，audit 直放，
    持有 FlowLock 互斥）。工具 schema 一律 OpenAI 兼容 `tools` 声明（§4.2，Pydantic 参数校验）。
    `engine` 缺省用真实库（`db_base.get_engine()`）；工具以 factory 闭包绑定该引擎。
    `lock/get_automation/pw_runner/executor` 透传给各 factory（测试注入假浏览器 / 假执行器）。
    """
    reg = ToolRegistry()
    reg.register(
        "echo",
        func=lambda text: {"echo": text, "received": True},
        description="回显工具（骨架假工具，验证决策环全链路）",
        schema={
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显工具（骨架假工具）",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        },
        write=False,
    )
    from agent.tools import build_browser_tools, build_read_tools, build_send_tools, build_write_tools

    eng = engine or db_base.get_engine()
    reg = build_read_tools(eng, reg)
    reg = build_write_tools(eng, reg)
    reg = build_browser_tools(eng, reg, lock=lock, get_automation=get_automation, pw_runner=pw_runner)
    return build_send_tools(
        eng, reg, executor=executor, lock=lock, get_automation=get_automation, pw_runner=pw_runner,
        paused=_runtime_paused,
    )


def echo_planner_factory(user_input: str):
    """确定性 planner：先调一次 echo，再收尾为 report（无需 AI key，curl 即可冒烟）。

    语义与 graph 测试的 `_make_planner` 一致：收到工具回灌后改走 report，动态续排。
    """

    def _planner(messages, tool_schemas):
        if not any(m.get("role") == "tool" for m in messages):
            return {"action": "tool", "name": "echo", "arguments": {"text": user_input}}
        return {"action": "report", "content": f"已回显：{user_input}"}

    return _planner


class ApprovalNotFoundError(ValueError):
    """decide 目标审批不存在 / 无关联会话线程 → 404。"""


class ApprovalAlreadyDecidedError(ValueError):
    """decide 目标审批已被处理（非 pending）→ 409。"""


def _default_checkpoint_path(engine) -> str:
    """生产缺省 checkpoint 文件：与数据库同目录（§6「SqliteSaver 与主库同目录」）。

    引擎无持久 DB 路径（内存库）时用临时目录 + 按引擎对象 id 分键，保证
    chat/decide 对同一 engine 复用到同一文件（跨调用 resume）。
    """
    db_path = getattr(engine.url, "database", None)
    if db_path and db_path != ":memory:":
        return os.path.join(os.path.dirname(db_path), "agent_checkpoint.sqlite")
    return os.path.join(tempfile.gettempdir(), f"agent_ckpt_{id(engine)}.sqlite")


def resolve_approval_for_decide(engine, approval_id: int, decision: str) -> str:
    """把审批行标为 approved/rejected（+decision + decided_at），返回其 graph_thread_id。

    审计审批门 decide 的第一步（§4.1/§4.3）：人工确定后先落库、再恢复挂起的图。
    - 审批不存在 / 无关联会话线程 → 抛 `ApprovalNotFoundError`；
    - 已处理（非 pending）→ 抛 `ApprovalAlreadyDecidedError`（幂等门，防重复决策）。
    事务内抛错会回滚已改的 status，不会留下半个决定。
    """
    with SASession(engine) as s, s.begin():
        ap = s.get(models.Approval, approval_id)
        if ap is None:
            raise ApprovalNotFoundError(f"审批 {approval_id} 不存在")
        if ap.status != state.ApprovalStatus.PENDING:
            raise ApprovalAlreadyDecidedError(f"审批 {approval_id} 已处理（status={ap.status}）")
        ap.status = (
            state.ApprovalStatus.APPROVED if decision == "approve" else state.ApprovalStatus.REJECTED
        )
        ap.decision = decision
        ap.decided_at = datetime.now()
        session = s.get(models.AgentSession, ap.session_id) if ap.session_id else None
        if session is None or not session.graph_thread_id:
            raise ApprovalNotFoundError(f"审批 {approval_id} 无关联会话线程")
        return session.graph_thread_id


def _thread_user_prompt(engine, thread_id: str) -> str:
    """取会话 user_prompt（resume 时依它重新派生 planner；拒绝回灌后要重跑 plan）。"""
    with SASession(engine) as s:
        row = s.execute(
            select(models.AgentSession).where(models.AgentSession.graph_thread_id == thread_id)
        ).scalar_one_or_none()
        return (row.user_prompt or "") if row is not None else ""


class AgentService:
    """决策环同步问答编排。planner / registry / engine / checkpointer 均可注入。

    Phase 3/4 把真实 planner（包 Step 2.2 `llm_chat_functions`）与真实工具注册表注入，
    或用子类复写，路由层不感知。
    """

    def __init__(
        self,
        *,
        engine=None,
        make_planner: Callable[[str], Any] | None = None,
        registry: ToolRegistry | None = None,
        checkpointer=None,
    ) -> None:
        self.engine = engine
        self.make_planner = make_planner
        self.registry = registry
        self.checkpointer = checkpointer

    async def chat(
        self,
        user_input: str,
        thread_id: str,
        execution_mode: str = "audit",
        *,
        on_step: Callable[[dict], Any] | None = None,
    ) -> dict:
        """跑一个同步问答回合（决策→执行→汇报→落库），返回对外响应字典。

        遇审计写工具挂起时返回 `status="pending_approval"` + `approval_pending`
        （含 approval_id），由 decide 端点接管（反而不返回 completed）。
        """
        out = await self._invoke(
            thread_id=thread_id,
            user_input=user_input,
            execution_mode=execution_mode,
            on_step=on_step,
        )
        return self._response(out, thread_id=thread_id)

    async def decide(self, approval_id: int, decision: str, *, on_step: Callable[[dict], Any] | None = None) -> dict:
        """审批门 decide（§5.1）：标记审批行 → 恢复挂起的图（Command(resume=decision)）。

        放行：工具执行继续；拒绝：图把"用户拒绝了 X"回灌 planner trace → Agent 改道/
        收尾（§4.3 reject ≠ 终止会话）。返回续跑后的响应字典（可能再次 pending）。
        """
        engine = self.engine or db_base.get_engine()
        thread_id = resolve_approval_for_decide(engine, approval_id, decision)
        out = await self._invoke(
            thread_id=thread_id,
            user_input=_thread_user_prompt(engine, thread_id),  # reject 后 replan 的 planner 入参
            execution_mode="audit",  # resume 不重建初态，此仅占位
            resume=decision,
            on_step=on_step,
        )
        return self._response(out, thread_id=thread_id)

    # ── 内部：建图 + 阻塞调用（跑在 to_thread 线程里）──
    async def _invoke(self, *, thread_id, user_input, execution_mode, resume: str | None = None, on_step=None):
        engine = self.engine or db_base.get_engine()
        registry = self.registry or default_registry(engine)
        # Step 6.2 缺省链：有 AI key 用真 LLM planner（agent/planner.py），无 key 回退 echo；
        # make_planner 显式注入优先（测试/定制零影响）。
        planner = (self.make_planner or default_planner_factory)(user_input)
        config = {"thread_id": thread_id, "recursion_limit": DEFAULT_RECURSION_LIMIT}

        if resume is None:
            def _run(compiled):
                return compiled.invoke(
                    {"thread_id": thread_id, "user_input": user_input, "execution_mode": execution_mode},
                    config,
                )
        else:
            def _run(compiled):  # noqa: F811
                return compiled.invoke(Command(resume=resume), config)

        def _build_and_run(checkpointer):
            compiled = build_agent_graph(
                planner=planner, registry=registry, engine=engine,
                checkpointer=checkpointer, on_step=on_step,
            )
            return _run(compiled)

        if self.checkpointer is not None:
            # 测试/注入的 saver
            return await asyncio.to_thread(_build_and_run, self.checkpointer)
        # 生产缺省：SqliteSaver 文件，chat/decide 各自在工作线程打开同文件 → 跨调用原地恢复

        def _prod():  # noqa: ANN202
            with SqliteSaver.from_conn_string(_default_checkpoint_path(engine)) as saver:
                return _build_and_run(saver)

        return await asyncio.to_thread(_prod)

    @staticmethod
    def _response(out: dict, *, thread_id: str) -> dict:
        """把图 invoke 输出转成对外响应；遇 `__interrupt__`（审计挂起）回 pending。"""
        inter = out.get("__interrupt__")
        if inter:
            first = inter[0] if isinstance(inter, (list, tuple)) else inter
            payload = getattr(first, "value", first) if not isinstance(first, dict) else first
            return {
                "thread_id": thread_id,
                "session_id": out.get("session_id"),
                "approval_pending": payload,
                "status": "pending_approval",
            }
        status = "ask_user" if out.get("ask_user_question") else "completed"
        return {
            "thread_id": thread_id,
            "session_id": out.get("session_id"),
            "report": out.get("report"),
            "ask_user_question": out.get("ask_user_question"),
            "status": status,
        }


__all__ = [
    "AgentService",
    "ApprovalNotFoundError",
    "ApprovalAlreadyDecidedError",
    "default_registry",
    "echo_planner_factory",
]
