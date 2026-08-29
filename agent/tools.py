"""agent/tools.py — Agent 只读工具（SDD Step 3.1，§4.2 工具清单）。

实现第一批"真"工具（相对 2.4 的 echo 假工具）：
- `query_jobs`：只读 DB 查岗位库，`status` 精确过滤 + `ungreeted=true` 专用过滤
  （打招呼流程的第一步必须先查库存再搜新，§4.2 硬规则）；可选 city/keyword/分页。
- `get_progress`：今日已投 / 每日额度 / 剩余额度（Agent 自律"额度用完就停"的前提）。

设计要点：
- **status 机映射**：`agent.state.JobStatus` 是 Agent 岗位状态机单一真源，值直接写/读
  现有 `applications.status` 列（与 dashboard 去重口径共享），工具不另立平行列。
- **L3 参数校验**：工具用 Pydantic 模型定义入参（§4.2），schema 经
  `interview.llm_client.build_tool_schema` 转 OpenAI tools 声明；执行时 `Model(**kwargs)`
  先校验再查库。**校验失败返回 `{"error": ...}` 结果而非抛异常**——错误作为工具输出
  回灌 plan，LLM 据此自纠（禁止 Agent 编默认值，§4.4）。
- **引擎注入**：工具以 factory 闭包绑定注入的 SQLA 引擎（测试用内存库、运行时真实库），
  `_execute_tool` 调 `func(**args)` 时 LLM 参数与 engine 无键冲突。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as SASession

from agent import state
from agent.graph import ToolRegistry
from db import models
from interview.llm_client import build_tool_schema

# ══════════════════════════════════════════════════════════
#  工具入参模型（§4.2：工具 schema 全部用 Pydantic 定义）
# ══════════════════════════════════════════════════════════


class QueryJobsParams(BaseModel):
    """query_jobs 入参。status 取 `agent.state.JobStatus` 词汇；ungreeted 与之互斥。"""

    status: str | None = Field(
        None, description="按 applications.status 精确过滤（discovered/pending/greeted/applied/replied/interview/filtered）"
    )
    ungreeted: bool | None = Field(
        False, description="只查可打招呼库存（status∈{pending,discovered}）；打招呼流程的第一步，与 status 互斥"
    )
    city: str | None = Field(None, description="按城市精确过滤（可选）")
    keyword: str | None = Field(None, description="岗位标题关键词模糊过滤（可选）")
    limit: int = Field(20, ge=1, le=100, description="返回条数上限（1-100）")
    offset: int = Field(0, ge=0, description="分页偏移")


class GetProgressParams(BaseModel):
    """get_progress 入参（当前无参）。"""


# ══════════════════════════════════════════════════════════
#  工具工厂（绑定引擎，闭包避免 LLM 参数键冲突）
# ══════════════════════════════════════════════════════════


def query_jobs_factory(engine) -> Callable[..., dict]:
    """构造 `query_jobs(**kwargs) -> dict`。只读 DB，write=False 在 audit 直接放行。"""

    def query_jobs(**kwargs: Any) -> dict:
        try:
            p = QueryJobsParams(**kwargs)
        except ValidationError as e:
            return {"error": "参数校验失败", "message": str(e)}

        if p.ungreeted and p.status is not None:
            return {"error": "参数互斥", "message": "ungreeted=true 与 status 互斥，不能同时指定"}
        if p.status is not None and p.status not in state.JobStatus.ALL:
            return {
                "error": "未知岗位状态",
                "message": f"status={p.status} 不在 Agent 状态机内",
                "valid": sorted(state.JobStatus.ALL),
            }

        filters = []
        if p.ungreeted:
            filters.append(models.Application.status.in_(sorted(state.JobStatus.GREETABLE)))
        if p.status is not None:
            filters.append(models.Application.status == p.status)
        if p.city:
            filters.append(models.Application.city == p.city)
        if p.keyword:
            filters.append(models.Application.job_title.contains(p.keyword))

        with SASession(engine) as s:
            total = s.execute(
                select(func.count()).select_from(models.Application).where(*filters)
            ).scalar_one()
            rows = s.execute(
                select(models.Application)
                .where(*filters)
                .order_by(models.Application.updated_at.desc())
                .limit(p.limit)
                .offset(p.offset)
            ).scalars().all()

        jobs = [
            {
                "id": r.id,
                "job_title": r.job_title,
                "company": r.company,
                "city": r.city,
                "salary": r.salary,
                "status": r.status,
                "hr_active_label": r.hr_active_label,
                "hr_active_days": r.hr_active_days,
                "job_url": r.job_url,
                "experience": r.experience,
                "education": r.education,
            }
            for r in rows
        ]
        return {"error": None, "total": total, "count": len(jobs), "jobs": jobs}

    return query_jobs


def get_progress_factory(engine) -> Callable[[], dict]:
    """构造 `get_progress() -> dict`：今日已投 / 每日额度 / 剩余额度 / 库存计数。

    有效上限 = min(daily_apply_limit 设置, MAX_APPLY_PER_DAY 硬上限)——与 apply_to_job
    的 `min(daily_limit, MAX_APPLY_PER_DAY)` 口径一致，避免"配置超上限仍报剩余额度"误导。
    """

    def get_progress() -> dict:
        with SASession(engine) as s:
            today = func.date(models.Application.greeting_sent_at) == text("date('now','localtime')")
            today_applied = s.execute(
                select(func.count()).select_from(models.Application).where(today)
            ).scalar_one()
            ungreeted_count = s.execute(
                select(func.count()).select_from(models.Application).where(
                    models.Application.status.in_(sorted(state.JobStatus.GREETABLE))
                )
            ).scalar_one()
            pending_count = s.execute(
                select(func.count()).select_from(models.Application).where(
                    models.Application.status == state.JobStatus.PENDING
                )
            ).scalar_one()
            discovered_count = s.execute(
                select(func.count()).select_from(models.Application).where(
                    models.Application.status == state.JobStatus.DISCOVERED
                )
            ).scalar_one()
            total = s.execute(
                select(func.count()).select_from(models.Application)
            ).scalar_one()

            row = s.get(models.Setting, "daily_apply_limit")
            try:
                daily_limit = int(row.value) if row is not None and row.value else 15
            except ValueError:
                daily_limit = 15

        from boss_automation import MAX_APPLY_PER_DAY  # noqa: PLC0415  （硬上限单一真源）

        effective_limit = min(daily_limit, MAX_APPLY_PER_DAY)
        return {
            "date": date.today().isoformat(),
            "today_applied": today_applied,
            "daily_limit": daily_limit,
            "effective_limit": effective_limit,
            "remaining": max(0, effective_limit - today_applied),
            "ungreeted_count": ungreeted_count,
            "pending_count": pending_count,
            "discovered_count": discovered_count,
            "total_applications": total,
        }

    return get_progress


# ══════════════════════════════════════════════════════════
#  只读工具注册表
# ══════════════════════════════════════════════════════════


def build_read_tools(engine, registry: ToolRegistry | None = None) -> ToolRegistry:
    """注册两个只读工具到 registry（缺省新建）。全部 write=False，audit 直放。"""
    reg = registry or ToolRegistry()
    reg.register(
        "query_jobs",
        func=query_jobs_factory(engine),
        description="查岗位库（只读）。打招呼流程的第一步必须是 query_jobs(ungreeted=true) 先查库存，再决定要不要搜新的（§4.2 硬规则）",
        schema=build_tool_schema("query_jobs", "查岗位库（只读，含 ungreeted 库存过滤）", QueryJobsParams),
        write=False,
    )
    reg.register(
        "get_progress",
        func=get_progress_factory(engine),
        description="查今日投递进度与剩余额度（只读）。额度用完就停（Agent 自律决策前提）",
        schema=build_tool_schema("get_progress", "查今日投递进度与剩余额度（只读）", GetProgressParams),
        write=False,
    )
    return reg


__all__ = [
    "QueryJobsParams",
    "GetProgressParams",
    "query_jobs_factory",
    "get_progress_factory",
    "build_read_tools",
]
