"""V1.3.0 勾选投递/批量删除 + 关键词筛选 验收。

1. **批量删除端点**（手动专属能力）：`POST /api/jobs/delete-batch` 按 job_url 把岗位置
   `filtered`（软删——默认列表/投递漏斗排除该状态，同岗位再搜到时 _persist_discovered
   自动恢复 pending，可反悔不丢数据）；不存在的 URL 计入 missing 不报错。
   直接 await 端点协程 + monkeypatch 数据层（不碰真实 DB 文件/不启动 Playwright）。
2. **dashboard 前端契约**（轻量断言，同 test_dashboard_agent_panel 思路——前端无构建链，
   不测 JS 运行时）：关键词过滤框 / 全选 / 投递勾选 / 删除勾选 / 勾选状态与操作函数 /
   审批卡 min-width（V1.3.0：shrink-to-fit 父级里百分比宽度退化，改 min-width 底宽语义）。
3. **Agent 关键词链路接线**：send_greetings 注册描述与 OPERATIONAL_RULES 都告知 LLM
   "没说关键词先反问、说了必传 keyword"（行为由真 LLM 冒烟验证，此处锁契约不回退）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent import planner
from agent.tools import SendGreetingsParams

# ══════════════════════════════════════════════════════════
#  1. 批量删除端点（软删 filtered）
# ══════════════════════════════════════════════════════════


def test_delete_batch_endpoint_soft_deletes(monkeypatch):
    import boss_app

    deleted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        boss_app, "get_application_by_url", lambda url: {"id": 7} if url.endswith("/a") else None
    )
    monkeypatch.setattr(
        boss_app, "update_application_status",
        lambda app_id, status, greeting_text=None: deleted.append((app_id, status)),
    )

    async def _no_broadcast(evt):
        pass

    monkeypatch.setattr(boss_app, "broadcast_ws", _no_broadcast)

    out = asyncio.run(
        boss_app.delete_jobs_batch(
            boss_app.DeleteJobsRequest(job_urls=["https://zhipin.example.com/a", "https://x/gone"])
        )
    )
    assert out == {"deleted": 1, "missing": 1}
    assert deleted == [(7, "filtered")]  # 软删：只置 filtered，不物理删行


# ══════════════════════════════════════════════════════════
#  2. dashboard 前端契约断言
# ══════════════════════════════════════════════════════════

DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_dashboard_has_keyword_filter_and_select_toolbar():
    html = _html()
    # 关键词过滤框（手动）+ 全选 + 投递/删除勾选按钮
    assert 'id="jobFilterInput"' in html
    assert 'id="jobCheckAll"' in html
    assert 'id="btnApplySelected"' in html and "applySelectedJobs" in html
    assert 'id="btnDeleteSelected"' in html and "deleteSelectedJobs" in html
    assert "/api/jobs/delete-batch" in html
    # 卡片勾选框 + 勾选状态集 + 过滤联动（renderJobs 内做内存过滤）
    assert "toggleJobCheck" in html and "jobChecked=new Set()" in html
    render_body = html.split("function renderJobs(jobs)")[1].split("function applyOne")[0]
    assert "jobFilterInput" in render_body and "_lastShownJobUrls" in render_body
    # 勾选投递复用既有串行批量（进度条 + 可取消），不走会带 30-90s 间隔的同步 apply-batch
    assert "doBatchApply(urls,'searchStatus')" in html


def test_dashboard_approval_card_uses_min_width():
    html = _html()
    # V1.3.0：shrink-to-fit 父级里 width:min(560px,100%) 的百分比会退化成内容宽（用户实测仍窄）
    # ——审批卡必须是 min-width 底宽语义（最小 420px，内容多动态变宽）
    assert "min-width:420px" in html
    assert "width:min(560px,100%)" not in html


# ══════════════════════════════════════════════════════════
#  3. Agent 关键词链路接线契约
# ══════════════════════════════════════════════════════════


def test_agent_keyword_contract_in_params_and_rules():
    # L3 参数：keyword 可选、有描述（LLM function-calling 靠描述决定何时传）
    schema = SendGreetingsParams.model_json_schema()["properties"]
    assert "keyword" in schema
    # 操作规则：投递前没说关键词先反问（3a 条），拿到后传 keyword；明确"都投"才不传
    assert "3a." in planner.OPERATIONAL_RULES and "keyword" in planner.OPERATIONAL_RULES
