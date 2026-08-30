"""SDD Step 2.3：决策图（LangGraph）契约验收（红→绿，先红）。

本文件先存在（红，`agent/graph.py` 尚未实现），实现后绿。覆盖 §4.1 决策图的核心承诺
+ §4.7 transcript 落库 + Step 2.2 function-calling 对接：

1. **全链路（决策→执行→汇报→落库）**：`echo` 假工具经 StateGraph 从 plan 决策 → execute
   执行 → 工具结果回灌 → 再次 plan 收尾为 report；`agent_steps` 全链路落库（plan/execute/
   report 每步一条），`agent_sessions.final_report` 写入、status 达成 completed。
2. **回灌校验**：两次 plan 之间，第二次 planner 收到的 messages 必然含第一次工具输出
   （工具结果回灌 plan 动态续排）。
3. **审计 interrupt + SqliteSaver 原地恢复**（§4.1 最大收益）：写工具在 audit 模式下
   `interrupt()` 挂起 → __interrupt__ 返回；**从同一 checkpoint 文件重新打开 saver**
   （模拟进程重启）→ `Command(resume="approve")` 恢复执行；审批行 pending→approved。
4. **拒绝回灌**：resume="reject" → 工具**不执行**、审批行 rejected，结果回灌 plan 令其
   换方案/收尾，report 给出"用户已拒绝"。
5. **熔断**（§4.1 recursion_limit）：planner 永不收尾时以可配 recursion_limit 抛
   `GraphRecursionError`；导出 `DEFAULT_RECURSION_LIMIT = 12`。
6. **反问**：plan 选 ask_user → 记 ask_user 步骤，本轮结束不调工具。

mock 策略：LLM 由注入的 `planner(messages, tool_schemas) -> decision` 假函数扮演
（记录收到的回灌上下文供断言）；checkpoint 用 SqliteSaver 临时文件以验证真实落盘+恢复；
transcript 落内存 SQLAlchemy 引擎（`models.Base.metadata.create_all`）。
"""

from __future__ import annotations

import langgraph  # noqa: F401  # 确保 langgraph 可导入（依赖已声明）
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agent import graph, state
from db import models

# ──────────────────────────────────────────────────────────
#  夹具
# ──────────────────────────────────────────────────────────


def _engine(tmp_path):
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    return eng


def _echo_tool_schema():
    return {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "回显工具（假工具）",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    }


def _make_planner(calls_log, *decisions):
    """把每次 planner 收到的回灌上下文 + 返回脚本化 decision。"""
    it = iter(decisions)

    def planner(messages, tool_schemas):
        calls_log.append({"messages": list(messages), "schemas": list(tool_schemas)})
        return next(it)

    return planner


# ──────────────────────────────────────────────────────────
#  验收 1+2：echo 全链路（决策→执行→汇报→落库）+ 回灌
# ──────────────────────────────────────────────────────────


def test_echo_chain_persists_transcript(tmp_path):
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    reg.register(
        "echo",
        func=lambda text: {"echo": text, "received": True},
        description="回显",
        schema=_echo_tool_schema(),
        write=False,
    )

    calls_log = []
    planner = _make_planner(
        calls_log,
        {"action": "tool", "name": "echo", "arguments": {"text": "你好"}},
        {"action": "report", "content": "收到：你好"},
    )
    app = graph.build_agent_graph(
        planner=planner, registry=reg, engine=eng, checkpointer=InMemorySaver()
    )

    out = app.invoke(
        {"thread_id": "t-echo", "user_input": "帮我回显一句话", "execution_mode": "audit"},
        config={"thread_id": "t-echo", "recursion_limit": graph.DEFAULT_RECURSION_LIMIT},
    )

    # 汇报落盘
    assert out["report"] == "收到：你好"
    assert out.get("ask_user_question") is None

    # 回灌：第二次 plan 必含第一次工具输出
    assert len(calls_log) == 2
    last_of_second = calls_log[1]["messages"][-1]
    assert last_of_second["role"] == "tool"
    assert "你好" in last_of_second["content"]

    # transcript：session 一条 + plan/execute/plan(回环)/report（§4.1 工具结果回灌动态续排）
    sid = _session_id(eng, "t-echo")
    steps = _steps(eng, sid)
    assert [s["kind"] for s in steps] == ["plan", "execute", "plan", "report"]
    exec_step = steps[1]
    assert exec_step["tool_name"] == "echo"
    assert exec_step["tool_input"]["text"] == "你好"
    assert exec_step["tool_output"]["echo"] == "你好"
    # 决策字面落库（§4.1 transcript 可回放）：第一次调 echo，第二次收尾为 report
    assert steps[0]["llm_decision"]["action"] == "tool"
    assert steps[2]["llm_decision"]["action"] == "report"

    # session 终态
    sess = _session(eng, "t-echo")
    assert sess["final_report"] == "收到：你好"
    assert sess["status"] == state.SessionStatus.COMPLETED


def test_echo_readonly_passes_in_audit_without_approval(tmp_path):
    """只读工具在 audit 模式直接执行，不留 pending 审批行。"""
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    reg.register("echo", func=lambda text: {"echo": text}, description="回显", schema=_echo_tool_schema(), write=False)
    planner = _make_planner(
        [],
        {"action": "tool", "name": "echo", "arguments": {"text": "x"}},
        {"action": "report", "content": "ok"},
    )
    app = graph.build_agent_graph(planner=planner, registry=reg, engine=eng, checkpointer=InMemorySaver())
    out = app.invoke(
        {"thread_id": "t-ro", "user_input": "回显", "execution_mode": "audit"},
        config={"thread_id": "t-ro", "recursion_limit": 12},
    )
    assert out["report"] == "ok"
    sid = _session_id(eng, "t-ro")
    assert _approvals(eng, sid) == []  # 只读不产生审批


# ──────────────────────────────────────────────────────────
#  验收 3：审计写工具 interrupt + SqliteSaver 原地恢复
# ──────────────────────────────────────────────────────────


def test_audit_write_interrupt_and_resume_across_restart(tmp_path):
    ckpt = tmp_path / "ckpt.db"
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    reg.register(
        "send_test",
        func=lambda to: {"sent": True, "to": to},
        description="假写工具",
        schema={
            "type": "function",
            "function": {
                "name": "send_test",
                "description": "假写",
                "parameters": {"type": "object", "properties": {"to": {"type": "string"}}},
            },
        },
        write=True,
    )
    planner = _make_planner(
        [],
        {"action": "tool", "name": "send_test", "arguments": {"to": "hr"}},
        {"action": "report", "content": "已发送"},
    )

    # 第一次：挂起（写工具 + audit → interrupt）
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        app = graph.build_agent_graph(planner=planner, registry=reg, engine=eng, checkpointer=saver)
        out = app.invoke(
            {"thread_id": "t-ap", "user_input": "发个测试", "execution_mode": "audit"},
            config={"thread_id": "t-ap", "recursion_limit": 12},
        )
    assert "__interrupt__" in out
    sid = _session_id(eng, "t-ap")
    # 挂起期间审批行是 pending，工具未执行
    assert _approvals(eng, sid)[0]["status"] == state.ApprovalStatus.PENDING
    assert not any(s["tool_name"] == "send_test" for s in _steps(eng, sid))

    # 第二次：从同一 checkpoint 文件全新打开 saver（模拟进程重启）→ approve 恢复
    with SqliteSaver.from_conn_string(str(ckpt)) as saver2:
        app2 = graph.build_agent_graph(planner=planner, registry=reg, engine=eng, checkpointer=saver2)
        out2 = app2.invoke(
            Command(resume="approve"),
            config={"thread_id": "t-ap", "recursion_limit": 12},
        )
    assert out2["report"] == "已发送"
    assert _approvals(eng, sid)[0]["status"] == state.ApprovalStatus.APPROVED
    assert any(s["tool_name"] == "send_test" and s["tool_output"]["sent"] for s in _steps(eng, sid))


# ──────────────────────────────────────────────────────────
#  验收 4：拒绝回灌（工具不执行）
# ──────────────────────────────────────────────────────────


def test_reject_skips_tool_and_replans(tmp_path):
    ckpt = tmp_path / "ckpt.db"
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    reg.register("send_test", func=lambda to: {"sent": True}, description="假写", write=True,
                 schema={"type": "function", "function": {"name": "send_test", "description": "假写",
                 "parameters": {"type": "object", "properties": {"to": {"type": "string"}}}}})
    planner = _make_planner(
        [],
        {"action": "tool", "name": "send_test", "arguments": {"to": "hr"}},
        {"action": "report", "content": "用户已拒绝，收尾"},
    )
    with SqliteSaver.from_conn_string(str(ckpt)) as saver:
        app = graph.build_agent_graph(
            planner=planner, registry=reg, engine=eng, checkpointer=saver
        )
        app.invoke(
            {"thread_id": "t-rj", "user_input": "发", "execution_mode": "audit"},
            config={"thread_id": "t-rj", "recursion_limit": 12},
        )
        out = app.invoke(Command(resume="reject"), config={"thread_id": "t-rj", "recursion_limit": 12})
    assert out["report"] == "用户已拒绝，收尾"
    sid = _session_id(eng, "t-rj")
    assert _approvals(eng, sid)[0]["status"] == state.ApprovalStatus.REJECTED
    assert not any(s["tool_name"] == "send_test" for s in _steps(eng, sid))  # 未执行


# ──────────────────────────────────────────────────────────
#  验收 5：recursion_limit 熔断
# ──────────────────────────────────────────────────────────


def test_recursion_limit_trips_neverending_planner(tmp_path):
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    reg.register("echo", func=lambda text: {"echo": text}, description="回显", schema=_echo_tool_schema(), write=False)

    def always_tool(messages, tool_schemas):  # noqa: ANN001, ANN201  # 永不收尾，触发熔断
        return {"action": "tool", "name": "echo", "arguments": {"text": "x"}}

    assert graph.DEFAULT_RECURSION_LIMIT == 12  # §4.1 默认熔断
    app = graph.build_agent_graph(planner=always_tool, registry=reg, engine=eng, checkpointer=InMemorySaver())
    try:
        app.invoke(
            {"thread_id": "t-loop", "user_input": "无限回显", "execution_mode": "audit"},
            config={"thread_id": "t-loop", "recursion_limit": 6},
        )
        raise AssertionError("应抛 GraphRecursionError")
    except GraphRecursionError:
        pass


# ──────────────────────────────────────────────────────────
#  验收 6：ask_user 反问路径
# ──────────────────────────────────────────────────────────


def test_ask_user_records_step_and_ends(tmp_path):
    eng = _engine(tmp_path)
    reg = graph.ToolRegistry()
    planner = _make_planner([], {"action": "ask_user", "question": "要投几个岗位？"})
    app = graph.build_agent_graph(planner=planner, registry=reg, engine=eng, checkpointer=InMemorySaver())
    out = app.invoke(
        {"thread_id": "t-ask", "user_input": "帮我投一下", "execution_mode": "audit"},
        config={"thread_id": "t-ask", "recursion_limit": 12},
    )
    assert out["ask_user_question"] == "要投几个岗位？"
    sid = _session_id(eng, "t-ask")
    assert [s["kind"] for s in _steps(eng, sid)] == ["plan", "ask_user"]
    assert _steps(eng, sid)[0]["llm_decision"]["action"] == "ask_user"


# ──────────────────────────────────────────────────────────
#  验收 7（Step 6.3 hotfix）：同 thread 多轮——新一轮输入必须进 planner 视野
# ──────────────────────────────────────────────────────────


def test_followup_turn_new_question_reaches_planner(tmp_path):
    """回归（用户实测：同 thread 追问不同问题，答案永远是首轮的）。

    旧 `_plan` 只在 trace 无 user 消息时注入用户输入（为回合内 replan 幂等设计），
    同一 thread 第二轮 invoke 的新 user_input 被吞——planner 只看到首轮上下文，
    问什么都答首轮；ask_user 的用户答复同样进不去。修复后：输入不在历史 user
    消息里 → 追加到 trace 末尾（回合内 replan 输入未变，依旧幂等跳过）。
    """
    eng = _engine(tmp_path)
    calls_log = []
    planner = _make_planner(
        calls_log,
        {"action": "report", "content": "首轮回答"},
        {"action": "report", "content": "次轮回答"},
    )
    app = graph.build_agent_graph(
        planner=planner, registry=graph.ToolRegistry(), engine=eng, checkpointer=InMemorySaver()
    )
    cfg = {"thread_id": "t-multi", "recursion_limit": graph.DEFAULT_RECURSION_LIMIT}

    out1 = app.invoke(
        {"thread_id": "t-multi", "user_input": "现在有多少待投递岗位", "execution_mode": "audit"}, config=cfg
    )
    out2 = app.invoke(
        {"thread_id": "t-multi", "user_input": "有哪些会话需要我回复", "execution_mode": "audit"}, config=cfg
    )

    assert out1["report"] == "首轮回答"
    assert out2["report"] == "次轮回答"
    # 第二轮 plan 收到的消息 = 首轮历史 + 本轮新问题（经 <user_input> 包裹）
    second = calls_log[1]["messages"]
    assert any(m.get("role") == "user" and "现在有多少待投递岗位" in m.get("content", "") for m in second)
    assert any(m.get("role") == "user" and "有哪些会话需要我回复" in m.get("content", "") for m in second)
    # transcript 仍按 session 顺序落库：两轮各一条 plan + report
    sid = _session_id(eng, "t-multi")
    assert [s["kind"] for s in _steps(eng, sid)] == ["plan", "report", "plan", "report"]


def test_ask_user_answer_reaches_planner_next_turn(tmp_path):
    """ask_user 续聊：用户答复（新 chat 调用）必须进第二轮 planner 视野。"""
    eng = _engine(tmp_path)
    calls_log = []
    planner = _make_planner(
        calls_log,
        {"action": "ask_user", "question": "要投几个岗位？"},
        {"action": "report", "content": "好的，投 5 个"},
    )
    app = graph.build_agent_graph(
        planner=planner, registry=graph.ToolRegistry(), engine=eng, checkpointer=InMemorySaver()
    )
    cfg = {"thread_id": "t-ans", "recursion_limit": graph.DEFAULT_RECURSION_LIMIT}
    app.invoke({"thread_id": "t-ans", "user_input": "帮我投岗位", "execution_mode": "audit"}, config=cfg)
    out = app.invoke({"thread_id": "t-ans", "user_input": "投 5 个", "execution_mode": "audit"}, config=cfg)

    assert out["report"] == "好的，投 5 个"
    second = calls_log[1]["messages"]
    # 次轮 planner 看到：反问（assistant）→ 用户答复（user）——完整对话链
    assert second[-1].get("role") == "user"
    assert "投 5 个" in second[-1]["content"]


# ──────────────────────────────────────────────────────────
#  只读查询小工具
# ──────────────────────────────────────────────────────────


def _session_id(eng, thread_id):
    with Session(eng) as s:
        return s.execute(
            _select(models.AgentSession).where(models.AgentSession.graph_thread_id == thread_id)
        ).scalar_one().id


def _session(eng, thread_id):
    from sqlalchemy import select

    with Session(eng) as s:
        row = s.execute(select(models.AgentSession).where(models.AgentSession.graph_thread_id == thread_id)).scalar_one()
        return {"final_report": row.final_report, "status": row.status}


def _select(model):
    from sqlalchemy import select

    return select(model)


def _steps(eng, sid):
    from sqlalchemy import select

    with Session(eng) as s:
        rows = s.execute(
            select(models.AgentStep).where(models.AgentStep.session_id == sid).order_by(models.AgentStep.id)
        ).scalars()
        return [
            {
                "kind": r.kind,
                "tool_name": r.tool_name,
                "tool_input": r.tool_input,
                "tool_output": r.tool_output,
                "llm_decision": r.llm_decision,
                "error": r.error,
            }
            for r in rows
        ]


def _approvals(eng, sid):
    from sqlalchemy import select

    with Session(eng) as s:
        rows = s.execute(
            select(models.Approval).where(models.Approval.session_id == sid).order_by(models.Approval.id)
        ).scalars()
        return [{"tool_name": r.tool_name, "status": r.status} for r in rows]
