"""agent/graph.py — Agent 决策循环 StateGraph（SDD Step 2.3，§4.1）。

用 LangGraph 搭建决策图，fulfill §4.1 的全部承诺：
- 节点链 `plan → (approval_gate →) execute_tool → 回环 → report/ask_user`；
- 审计模式写工具在 `approval_gate` 用原生 `interrupt()` 挂起，`Command(resume=...)`
  放行，checkpoint 使「进程重启后挂起会话原地恢复」；
- `recursion_limit` 熔断（默认 12）替代手写 max_steps；
- 每步落到 `agent_steps`（transcript，§4.7），汇报写回 `agent_sessions.final_report`；
- **脱敏（§3.2/§4.3）**：落库 / WS 外发 / 审批展示前对 tool_input、tool_output、
  llm_decision、trace 统一跑 `state.mask_sensitive`——api_key/wechat_id/手机号在
  transcript 与日志一律掩码，持久层不留原始密钥（Step 3.2 引入）。

**plan 节点**的 LLM 由注入的 `planner(messages, tool_schemas) -> decision` 扮演：
  {"action": "tool",  "name": str, "arguments": dict}      → 调工具（走执行链）
  {"action": "ask_user", "question": str}                  → 反问，本轮结束
  {"action": "report", "content": str}                     → 汇总收尾
真实 planner（Phase 3 接 `llm_chat_functions` 走 ToolRegistry）复用同一契约，测试用假
planner 驱动全链路（决策→执行→汇报→落库）。工具只能调 ToolRegistry 白名单里的、
参数已校验——安全边界 L3 的承载点（执行时 `func(**arguments)`）。
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from agent import defense, state
from db import models

DEFAULT_RECURSION_LIMIT = 12  # §4.1 熔断：替代手写 max_steps


# ══════════════════════════════════════════════════════════
#  Graph 状态
# ══════════════════════════════════════════════════════════


class AgentState(TypedDict, total=False):
    """StateGraph 节点共享状态（含 LangGraph 原生检查点持久化的全部业务字段）。"""

    thread_id: str
    session_id: int
    user_input: str
    trace: list[dict]  # planner 可见的消息历史（user/assistant/tool），回灌据此动态续排
    decision: dict  # planner 最新一步（tool / ask_user / report）
    approval_result: bool
    tool_result: dict | None
    report: str | None
    ask_user_question: str | None
    last_plan_step_id: int | None
    execution_mode: str  # audit 审计默认 / autonomous 全权（§4.3）


# ══════════════════════════════════════════════════════════
#  ToolRegistry（§4.2 白名单注册制，安全边界 L3）
# ══════════════════════════════════════════════════════════


class _Tool:
    __slots__ = ("name", "func", "schema", "write")

    def __init__(self, name: str, func, schema: dict, write: bool):
        self.name = name
        self.func = func
        self.schema = schema
        self.write = write


class ToolRegistry:
    """工具白名单：LLM 只能调注册过的工具、传校验过的参数。

    `write=True` 的工具在 audit 模式下必须过审批门（interrupt）才执行；
    只读工具直接放行。registry 向 plan 节点暴露 OpenAI 兼容 tools schema 列表。
    """

    def __init__(self) -> None:
        self._tools: dict[str, _Tool] = {}

    def register(
        self,
        name: str,
        func,
        *,
        description: str = "",
        schema: dict | None = None,
        write: bool = False,
    ) -> None:
        if schema is None:
            schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        self._tools[name] = _Tool(name, func, schema, write)

    def get(self, name: str) -> _Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [t.schema for t in self._tools.values()]


# ══════════════════════════════════════════════════════════
#  transcript / session 落库（§4.7，SQLA ORM 保证 JSON 列类型化存取）
# ══════════════════════════════════════════════════════════


def _ensure_session(engine, thread_id: str, user_input: str, exec_mode: str) -> int:
    with SASession(engine) as s:
        row = s.execute(
            select(models.AgentSession).where(models.AgentSession.graph_thread_id == thread_id)
        ).scalar_one_or_none()
        if row is not None:
            return row.id
        s.add(
            models.AgentSession(
                graph_thread_id=thread_id,
                execution_mode=exec_mode,
                user_prompt=user_input,
                status=state.SessionStatus.ACTIVE,
            )
        )
        s.commit()
        return s.execute(
            select(models.AgentSession).where(models.AgentSession.graph_thread_id == thread_id)
        ).scalar_one().id


def _persist_step(
    engine,
    session_id: int,
    kind: str,
    *,
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_output: Any = None,
    llm_decision: Any = None,
) -> int:
    with SASession(engine) as s, s.begin():
        st = models.AgentStep(
            session_id=session_id,
            kind=kind,
            tool_name=tool_name,
            # §3.2 脱敏：transcript 落库前掩码敏感值（api_key/wechat_id/手机号），不留原始密钥
            tool_input=state.mask_sensitive(tool_input),
            tool_output=state.mask_sensitive(tool_output),
            llm_decision=state.mask_sensitive(llm_decision),
            status=state.StepStatus.DONE,
        )
        s.add(st)
        s.flush()
        return int(st.id)


def _create_approval(engine, session_id: int, step_id: int | None, tool_name: str, tool_input: Any) -> int:
    with SASession(engine) as s, s.begin():
        ap = models.Approval(
            session_id=session_id,
            step_id=step_id,
            tool_name=tool_name,
            tool_input=state.mask_sensitive(tool_input),  # §3.2：审批行也不落原始密钥
            status=state.ApprovalStatus.PENDING,
        )
        s.add(ap)
        s.flush()
        return int(ap.id)


def _get_or_create_approval(engine, session_id: int, step_id: int | None, tool_name: str, tool_input: Any) -> tuple[int, bool]:
    """"审计挂起"审批行的幂等创建，返回 `(approval_id, is_new)`。

    LangGraph 恢复时会把整个节点函数体从头再跑一遍（interrupt() 返回 resume 值后
    继续执行），因此本函数会被执行两次——用 `(session_id, tool_name, step_id)`
    定位既有行复用之（**不受审批行当前 status 限制**，见 §5.1）：decide API 在恢复前
    已把行标成 approved/rejected，若仍按 PENDING 过滤就会在 replay 时新建第二行。
    `is_new` 供 approval_gate 仅在新创建（首次挂起）时发一次审批的 WS 事件，避免
    replay 重复通知。
    """
    with SASession(engine) as s:
        existing = (
            s.execute(
                select(models.Approval)
                .where(
                    models.Approval.session_id == session_id,
                    models.Approval.tool_name == tool_name,
                    models.Approval.step_id == step_id,
                )
                .order_by(models.Approval.id)
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return int(existing.id), False
    return _create_approval(engine, session_id, step_id, tool_name, tool_input), True


def _set_approval(engine, approval_id: int, status: str) -> None:
    with SASession(engine) as s, s.begin():
        row = s.get(models.Approval, approval_id)
        if row is not None:
            row.status = status


def _finalize_session(engine, session_id: int, report: str) -> None:
    with SASession(engine) as s, s.begin():
        row = s.get(models.AgentSession, session_id)
        if row is not None:
            row.final_report = report
            row.status = state.SessionStatus.COMPLETED


# ══════════════════════════════════════════════════════════
#  节点实现（闭包捕获 planner / registry / engine）
# ══════════════════════════════════════════════════════════


def build_agent_graph(*, planner, registry: ToolRegistry, engine, checkpointer=None, on_step=None):
    """构造并编译决策图。

    - `planner(messages, tool_schemas) -> decision`：LLM function-calling 决策接缝；
    - `registry`：工具白名单（execute 从这里取工具执行）；
    - `engine`：SQLA 引擎，作 session/step/approval transcript 落库（§4.7）；
    - `checkpointer`：LangGraph 检查点（缺省 InMemorySaver；生产用
      `SqliteSaver`，实现窗口恢复 §4.1）。execution_mode 在 invoke 输入里按会话传入。
    - `on_step(event) -> None`：可选回调，每完成一步（plan/execute/ask_user/report）
      触发一次，event 含 kind/step_id/tool_name/tool_input/llm_decision——Step 2.4
      对话 API 据此经 WebSocket 推送步骤进度（§3 对话入口）。缺省 None（2.3 行为不变）。
    """
    def _notify(kind: str, *, step_id: int, tool_name=None, tool_input=None, llm_decision=None) -> None:
        """每步完成回调：Step 2.4 WS 进度推送的使能点（缺省 on_step=None 为 no-op）。

        §3.2 脱敏：外发到前端的 tool_input/llm_decision 先掩码敏感值。
        """
        if on_step is None:
            return
        event: dict = {"kind": kind, "step_id": step_id}
        if tool_name is not None:
            event["tool_name"] = tool_name
        if tool_input is not None:
            event["tool_input"] = state.mask_sensitive(tool_input)
        if llm_decision is not None:
            event["llm_decision"] = state.mask_sensitive(llm_decision)
        on_step(event)

    # ── plan：LLM 决策 + 决策字面落库 ──
    def _plan(st: AgentState) -> dict:
        sid = _ensure_session(engine, st["thread_id"], st.get("user_input", ""), st.get("execution_mode", "audit"))
        trace = list(st.get("trace", []))
        # L0 隔离（Step 5.2）：首次把用户输入经 <user_input>…</user_input> 数据化注入 trace，
        # 后续 replan 已有 user 消息即跳过（幂等）——所有 planner 看到正确的"数据非指令"边界。
        if not any(m.get("role") == "user" for m in trace):
            trace = [{"role": "user", "content": defense.wrap_user_input(st.get("user_input", ""))}] + trace
        decision = planner(messages=trace, tool_schemas=registry.schemas())
        step_id = _persist_step(engine, sid, state.StepKind.PLAN, llm_decision=decision)
        _notify(state.StepKind.PLAN, step_id=step_id, llm_decision=decision)
        return {
            "session_id": sid,
            "decision": decision,
            "last_plan_step_id": step_id,
            # §3.2 脱敏：回灌/checkpoint 的 trace 掩码敏感值（执行仍用 st["decision"] 原始参）
            "trace": trace + [{"role": "assistant", "decision": state.mask_sensitive(decision)}],
        }

    # ── approval_gate：审计写工具 interrupt，放行/拒绝 ──
    def _approval_gate(st: AgentState) -> dict:
        dec = st["decision"]
        name = dec["name"]
        args = dec.get("arguments", {})
        tool = registry.get(name)
        grants = True
        extra: dict = {}
        if tool is not None and tool.write and st.get("execution_mode", "audit") == "audit":
            ap_id, is_new = _get_or_create_approval(
                engine, st["session_id"], st.get("last_plan_step_id"), name, args
            )
            # §5.1 WS 审批通知：仅首次挂起发一次（replay 幂等复用，is_new=False 不重复）
            if is_new:
                _notify(state.StepKind.APPROVAL, step_id=ap_id, tool_name=name, tool_input=args)
            # §5.1 interrupt 载荷带 approval_id：chat 返回 pending_approval 时前端据其调 decide
            verdict = interrupt({  # §3.2：展示给人工前掩码
                "tool": name, "arguments": state.mask_sensitive(args), "approval_id": ap_id,
            })
            if verdict == "approve":
                _set_approval(engine, ap_id, state.ApprovalStatus.APPROVED)
            else:
                _set_approval(engine, ap_id, state.ApprovalStatus.REJECTED)
                grants = False
                reason = verdict if isinstance(verdict, str) else "未批准"
                extra["trace"] = st.get("trace", []) + [
                    {"role": "tool", "content": f"用户拒绝了工具 {name}（{reason}）。请另选方案或收尾。"}
                ]
        return {"approval_result": grants, **extra}

    # ── execute_tool：调 ToolRegistry，入参出参落库，结果回灌 trace ──
    def _execute_tool(st: AgentState) -> dict:
        dec = st["decision"]
        name = dec["name"]
        args = dec.get("arguments", {})
        tool = registry.get(name)
        if tool is None:
            # Step 6.2：真 LLM 可能幻觉出未注册工具名——error dict 回灌自纠（§3.1 先例，
            # 与工具 Pydantic 校验失败同模式），不再抛 KeyError 炸整个回合；allowed 带
            # 白名单供 LLM 修正后重试。
            out: dict = {"error": f"未注册的工具：{name}", "allowed": registry.names()}
        else:
            out = tool.func(**args)
        step_id = _persist_step(
            engine, st["session_id"], state.StepKind.EXECUTE,
            tool_name=name, tool_input=args, tool_output=out,
        )
        _notify(state.StepKind.EXECUTE, step_id=step_id, tool_name=name, tool_input=args, llm_decision=dec)
        resp = {"tool": name, "output": out}
        # §3.2 脱敏后再回灌；Step 5.2：工具输出是**不可信输入** → L1 包 <untrusted>…</untrusted>，
        # L2 跑注入检测（命中记 WARNING；REJECT_FEEDBACK_ON_HIT 开启时可拒绝回灌 LLM）。
        untrusted = json.dumps(state.mask_sensitive(resp), ensure_ascii=False)
        if defense.should_reject_feedback(untrusted):
            untrusted = defense.wrap_untrusted("[已拦截注入内容，未回灌]")
        else:
            untrusted = defense.wrap_untrusted(untrusted)
            defense.detect_injection(untrusted)  # 命中即 WARNING 日志（默认只记不拦）
        return {
            "tool_result": resp,
            "trace": st.get("trace", []) + [{"role": "tool", "content": untrusted}],
        }

    # ── ask_user：反问结束本轮（§4.4，禁止编默认值）──
    def _ask_user(st: AgentState) -> dict:
        q = st["decision"].get("question", "")
        ask_step_id = _persist_step(engine, st["session_id"], state.StepKind.ASK_USER, llm_decision=st["decision"])
        _notify(state.StepKind.ASK_USER, step_id=ask_step_id, llm_decision=st["decision"])
        return {"ask_user_question": q}

    # ── report：最终汇报 + session 收尾 ──
    def _report(st: AgentState) -> dict:
        if st["decision"].get("action") != "report":
            content = st.get("report") or ""
        else:
            content = st["decision"].get("content") or ""
        # L5 输出过滤（Step 5.2）：最终回复出口——落库与查询报告前过滤 system prompt 内容、
        # 完整 api_key、密钥类 setting 值（命中掩码/替换，正常回复原样放行）。
        content = defense.sanitize_output(
            content, secrets=defense.collect_sensitive_values(engine)
        )
        report_step_id = _persist_step(engine, st["session_id"], state.StepKind.REPORT, llm_decision=st["decision"])
        _notify(state.StepKind.REPORT, step_id=report_step_id, llm_decision=st["decision"])
        _finalize_session(engine, st["session_id"], content)
        return {"report": content}

    def _route_after_plan(st: AgentState) -> str:
        action = st["decision"].get("action")
        if action == "tool":
            return "approval_gate"
        if action == "ask_user":
            return "ask_user"
        return "report"

    def _route_after_approval(st: AgentState) -> str:
        return "execute_tool" if st.get("approval_result", True) else "plan"

    g = StateGraph(AgentState)
    g.add_node("plan", _plan)
    g.add_node("ask_user", _ask_user)
    g.add_node("approval_gate", _approval_gate)
    g.add_node("execute_tool", _execute_tool)
    g.add_node("report", _report)

    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan",
        _route_after_plan,
        {"approval_gate": "approval_gate", "ask_user": "ask_user", "report": "report"},
    )
    g.add_conditional_edges(
        "approval_gate",
        _route_after_approval,
        {"execute_tool": "execute_tool", "plan": "plan"},
    )
    g.add_edge("execute_tool", "plan")  # 工具完成 → 回环 plan 动态续排
    g.add_edge("ask_user", END)
    g.add_edge("report", END)

    if checkpointer is None:
        checkpointer = InMemorySaver()
    return g.compile(checkpointer=checkpointer)


__all__ = [
    "AgentState",
    "ToolRegistry",
    "build_agent_graph",
    "DEFAULT_RECURSION_LIMIT",
]
