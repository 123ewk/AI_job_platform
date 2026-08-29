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
from typing import Any, Callable

from agent.graph import DEFAULT_RECURSION_LIMIT, ToolRegistry, build_agent_graph
from db import base as db_base


def _runtime_paused() -> bool:
    """运行时用户暂停标志（懒加载 boss_app.monitor_paused，避免 registry 构建期 import boss_app）。"""
    from boss_app import monitor_paused  # noqa: PLC0415

    return bool(monitor_paused)


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


class AgentService:
    """决策环同步问答编排。planner / registry / engine / checkpointer 均可注入。

    Phase 3/4 把真实 planner（包 Step 2.2 `llm_chat_functions`）与真实工具注册表注入，
    或用子类复写 `chat`，路由层不感知。
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
        """跑一个同步问答回合（决策→执行→汇报→落库），返回对外响应字典。"""
        engine = self.engine or db_base.get_engine()
        registry = self.registry or default_registry(engine)
        planner = (self.make_planner or echo_planner_factory)(user_input)
        compiled = build_agent_graph(
            planner=planner,
            registry=registry,
            engine=engine,
            checkpointer=self.checkpointer,
            on_step=on_step,
        )
        out = await asyncio.to_thread(
            compiled.invoke,
            {
                "thread_id": thread_id,
                "user_input": user_input,
                "execution_mode": execution_mode,
            },
            {"thread_id": thread_id, "recursion_limit": DEFAULT_RECURSION_LIMIT},
        )
        status = "ask_user" if out.get("ask_user_question") else "completed"
        return {
            "thread_id": thread_id,
            "session_id": out.get("session_id"),
            "report": out.get("report"),
            "ask_user_question": out.get("ask_user_question"),
            "status": status,
        }


__all__ = ["AgentService", "default_registry", "echo_planner_factory"]
