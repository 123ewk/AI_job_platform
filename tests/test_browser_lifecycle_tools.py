"""Agent 浏览器生命周期工具 + 执行前预检回归。

用户需求：Agent 执行浏览器类工具前先确认浏览器是否启动；Agent 自带
open_browser / close_browser 工具做自愈。预检命中时必须秒返「可自愈」引导报错
（引导 planner 先调 open_browser 再重试），不真进工具撞 TargetClosedError。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine

from agent import graph, planner
from agent.flow_lock import FlowLock
from agent.tools import build_browser_tools, build_read_tools, build_send_tools
from db import models

# ──────────────────────────────────────────────────────────
#  夹具
# ──────────────────────────────────────────────────────────


class _FakeAutomation:
    """假浏览器对象：page 在/心跳成败/关闭可记录，覆盖 open/close 三个场景。"""

    def __init__(self, *, with_page=True, heartbeat_ok=True):
        self.page = object() if with_page else None
        self._heartbeat_ok = heartbeat_ok
        self.closed = False

    def heartbeat(self):
        if not self._heartbeat_ok:
            raise RuntimeError("Target page, context or browser has been closed")
        return True

    def close(self):
        self.closed = True


def _sync_runner(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _registry():
    eng = create_engine("sqlite://")
    return build_browser_tools(
        eng,
        lock=FlowLock(),
        get_automation=lambda: None,
        pw_runner=_sync_runner,
        set_automation=lambda a: None,
        start_browser=lambda: _FakeAutomation(),
    )


# ──────────────────────────────────────────────────────────
#  注册契约：新工具入册 + requires_browser 标记
# ──────────────────────────────────────────────────────────


def test_browser_lifecycle_tools_registered_readonly():
    reg = _registry()
    for name in ("open_browser", "close_browser"):
        tool = reg.get(name)
        assert tool is not None, f"缺少 {name} 工具"
        assert tool.write is False  # 生命周期操作不走审批门（护栏在工具内部）


def test_browser_tools_flagged_for_precheck():
    eng = create_engine("sqlite://")
    reg = build_browser_tools(
        eng, lock=FlowLock(), get_automation=lambda: None, pw_runner=_sync_runner, set_automation=lambda a: None
    )
    assert reg.get("search_jobs").requires_browser is True
    assert reg.get("get_conversations_summary").requires_browser is False

    reg2 = build_send_tools(eng, executor=None, lock=FlowLock(), get_automation=lambda: None, pw_runner=_sync_runner)
    assert reg2.get("send_greetings").requires_browser is True

    reg3 = build_read_tools(eng)
    assert reg3.get("query_jobs").requires_browser is False


# ──────────────────────────────────────────────────────────
#  open_browser：未启动 / 已运行 / 窗口被关自愈
# ──────────────────────────────────────────────────────────


def test_open_browser_starts_when_absent():
    started_with = []
    setter_val = []
    tool = _make_open(get_automation=lambda: None, starter=lambda: started_with.append(1) or "NEW", setter=setter_val.append)
    out = tool()
    assert out["status"] == "started" and out["error"] is None
    assert started_with == [1] and setter_val == ["NEW"]


def test_open_browser_idempotent_when_healthy():
    calls = {"start": 0}
    healthy = _FakeAutomation()
    tool = _make_open(
        get_automation=lambda: healthy,
        starter=lambda: calls.__setitem__("start", calls["start"] + 1) or _FakeAutomation(),
    )
    out = tool()
    assert out["status"] == "already_running"
    assert calls["start"] == 0 and not healthy.closed


def test_open_browser_heals_stale_window():
    stale = _FakeAutomation(heartbeat_ok=False)  # 对象在、窗口已死
    fresh = _FakeAutomation()
    setter_val = []
    tool = _make_open(get_automation=lambda: stale, starter=lambda: fresh, setter=setter_val.append)
    out = tool()
    assert out["status"] == "started"
    assert stale.closed and setter_val == [fresh]


def _make_open(*, get_automation, starter, setter=lambda a: None):
    return build_browser_tools(
        create_engine("sqlite://"),
        lock=FlowLock(),
        get_automation=get_automation,
        pw_runner=_sync_runner,
        set_automation=setter,
        start_browser=starter,
    ).get("open_browser").func


# ──────────────────────────────────────────────────────────
#  close_browser：未启动 / 浏览器忙 / 正常关闭
# ──────────────────────────────────────────────────────────


def _make_close(*, get_automation, lock=None, setter=lambda a: None):
    return build_browser_tools(
        create_engine("sqlite://"),
        lock=lock or FlowLock(),
        get_automation=get_automation,
        pw_runner=_sync_runner,
        set_automation=setter,
    ).get("close_browser").func


def test_close_browser_noop_when_not_running():
    closed = []
    tool = _make_close(get_automation=lambda: None, setter=lambda a: closed.append(a))
    out = tool()
    assert out["status"] == "not_running" and closed == []


def test_close_browser_refuses_while_flow_lock_held():
    lock = FlowLock()
    assert lock.acquire("test:task")
    target = _FakeAutomation()
    tool = _make_close(get_automation=lambda: target, lock=lock)
    out = tool()
    assert out["error"] == "浏览器忙"
    assert not target.closed
    lock.release()


def test_close_browser_closes_and_clears_global():
    target = _FakeAutomation()
    setter_val = []
    tool = _make_close(get_automation=lambda: target, setter=setter_val.append)
    out = tool()
    assert out["status"] == "closed"
    assert target.closed and setter_val == [None]


# ──────────────────────────────────────────────────────────
#  graph execute_tool 预检：未启动秒返引导报错，不进工具
# ──────────────────────────────────────────────────────────


def _engine(tmp_path):
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)
    return eng


def _make_planner(*decisions):
    it = iter(decisions)

    def planner_fn(messages, tool_schemas):
        return next(it)

    return planner_fn


def _invoke_with_browser_tool(tmp_path, monkeypatch, automation):
    """注册 requires_browser 假工具并跑一轮回合，返回 (工具调用次数, execute 输出)。"""
    from agent.tools import _default_get_automation as _unused_real  # noqa: F401

    monkeypatch.setattr("agent.tools._default_get_automation", lambda: automation)

    calls = {"n": 0}

    def fake_browser_tool(**kwargs):
        calls["n"] += 1
        return {"ok": True}

    reg = graph.ToolRegistry()
    reg.register(
        "fake_search",
        func=fake_browser_tool,
        description="假浏览器工具",
        write=False,
        requires_browser=True,
    )
    app = graph.build_agent_graph(
        planner=_make_planner(
            {"action": "tool", "name": "fake_search", "arguments": {}},
            {"action": "report", "content": "收尾"},
        ),
        registry=reg,
        engine=_engine(tmp_path),
        checkpointer=InMemorySaver(),
    )
    out = app.invoke(
        {"thread_id": "t-pre", "user_input": "搜一下岗位", "execution_mode": "audit"},
        config={"thread_id": "t-pre", "recursion_limit": graph.DEFAULT_RECURSION_LIMIT},
    )
    return calls["n"], out


def test_graph_precheck_blocks_tool_when_browser_absent(tmp_path, monkeypatch):
    n, out = _invoke_with_browser_tool(tmp_path, monkeypatch, automation=None)
    assert n == 0  # 工具根本没进
    assert out["tool_result"]["output"]["error"] == "浏览器未启动"
    # 回灌里带 open_browser 自愈指引，planner 据此改道
    assert any("open_browser" in m.get("content", "") for m in out["trace"] if m.get("role") == "tool")


def test_graph_precheck_passes_when_browser_ready(tmp_path, monkeypatch):
    n, out = _invoke_with_browser_tool(tmp_path, monkeypatch, automation=_FakeAutomation())
    assert n == 1
    assert out["tool_result"]["output"] == {"ok": True}


# ──────────────────────────────────────────────────────────
#  planner 规则契约：自愈指引写进系统提示
# ──────────────────────────────────────────────────────────


def test_planner_rules_mention_browser_lifecycle():
    assert "open_browser" in planner.OPERATIONAL_RULES
    assert "close_browser" in planner.OPERATIONAL_RULES
