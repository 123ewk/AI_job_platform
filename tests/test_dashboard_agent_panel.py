"""dashboard Agent 对话面板回归（SDD Step 6.3）。

轻量断言：static/dashboard.html 引用了 Agent 对话面板所需的端点 / WS / 事件契约，
防止后续改版把面板拆断（不测 JS 运行时行为——前端无构建链，跑不起来 pytest）。
"""

from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_dashboard_has_agent_tab_and_http_endpoints():
    html = _html()
    # 侧边栏新 tab + 对话/审批/停止三组端点（Step 6.3 spec）
    assert 'data-tab="agent"' in html
    assert "/api/agent/chat" in html
    assert "/api/agent/approvals/" in html and "/decide" in html
    assert "/api/agent/tasks/" in html and "/stop" in html


def test_dashboard_connects_agent_ws_and_handles_event_contract():
    html = _html()
    # WS 通道 + Step 2.4/4.5/5.1 事件契约
    assert "/ws/agent" in html
    for evt in ("agent_connected", "agent_step", "agent_chat_done", "agent_task_progress", "agent_task_done"):
        assert evt in html, f"dashboard 缺少 WS 事件处理：{evt}"
    # 步骤时间线覆盖五种 step（agent.state.StepKind）
    for kind in ("plan", "execute", "approval", "ask_user", "report"):
        assert kind in html, f"dashboard 步骤时间线缺少 kind：{kind}"


def test_dashboard_has_execution_mode_and_decide_payload():
    html = _html()
    # 执行模式下拉（§4.3 audit 默认）+ decide 请求体契约（approve/reject 两态）
    assert "audit" in html and "autonomous" in html
    assert "decideAgentApproval" in html
    assert "approve" in html and "reject" in html


def test_dashboard_startup_runs_after_top_level_declarations():
    # 回归：启动行若在顶层 let/const（appAllJobs/PLATFORMS/agentWs…）声明之前执行，
    # 会踩 TDZ 抛 ReferenceError 中断整个脚本——岗位列表与 Agent 面板同时失效（0bb73a8 引入）。
    html = _html()
    assert html.rindex("agentWsConnect();") > html.index("let agentWs=null")
    assert html.rindex("agentWsConnect();") > html.index("let appCurrentPage")


def test_agent_bubble_renders_inline_markdown():
    # V1.2.26：模型汇报带 **加粗**/`代码`，气泡要渲染而不是星号直出
    assert "agentMd" in _html()
