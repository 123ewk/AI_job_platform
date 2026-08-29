"""agent/recovery.py — 崩溃恢复（SDD Step 4.3，§4.5/§8.3）。

后台打招呼任务可能在进程被拔电源/杀死时中断：`_run` 的 finally 不执行（真实 kill 不跑
finally），于是 `agent_tasks` 会遗留非终态（pending/running）行，且在途岗位可能**已发送
但未落库 `greeted`**——若直接当作待投库存续投会造成**重复打招呼**。

本模块做两件事（都是**幂等**的，可安全重复调用）：

1. `recover_interrupted_tasks(engine)`：启动时恢复——把非终态任务统一标 `interrupted`（终态，
   任务本体不再复活，续投由 Agent 提议新建任务完成）。对 running 任务，唯一"结果未知"
   的岗位是**在途那一个**：`agent_tasks.progress_done` 在单位**完成后**才落，故在途单位
   （1 基） = 已完成单位数 + 1 = 0 基下标 `jobs[progress_done]` → 将其 `applications.status`
   置为 `state.JobStatus.UNKNOWN`。
   - 已完成单位（1..progress_done）已 `_mark_greeted` 落库 → **安全**；
   - 未开始单位 → **安全**（可在续投时照常放出）；
   - 仅这一个在途岗位发送结果未知 → 隔离等待人工确认。`UNKNOWN` 天然不在
     `GREETABLE={pending,discovered}` → query_jobs(ungreeted)/send_greetings 库存自动排除
     → **无重复发送**。
   - pending 任务（`_run` 从未启动）任何岗位都未触碰 → 不隔离。

2. `resolve_unknown_result(engine, *, application_id, sent_confirm, greeting)`：**人工确认门**。
   unknown 岗位必须人工确认才可重发：
   - `sent_confirm=True`（打招呼确实发出）→ 置 `greeted`（+ 招呼语 + 时间戳，无重复发送）；
   - `sent_confirm=False`（未发出）→ 回 `pending`（进 GREETABLE，可安全重发）。
   非 unknown 岗位拒绝（幂等门，避免误清）。

入口：`TaskExecutor.recover()`（见 agent/executor.py）+ `agent.api._get_executor` 每进程建
执行器时调一次，即"启动时"。广播可选（生产接 AgentHub → /ws/agent，观察恢复事件）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session as SASession

from agent import state
from db import models

logger = logging.getLogger("agent.recovery")

__all__ = ["recover_interrupted_tasks", "resolve_unknown_result"]


def recover_interrupted_tasks(engine, *, broadcast: Callable[[dict], Any] | None = None) -> dict:
    """启动崩溃恢复：非终态任务标 interrupted；running 任务在途岗位置 unknown（结果未知隔离）。

    幂等：终态任务不在扫描集内，重复调用 0 新增动作。返回恢复报告：
    `{"interrupted": int, "unknown_jobs": [...], "safe_pending": int}`。
    """
    interrupted = 0
    unknown_jobs: list[dict] = []
    with SASession(engine) as s:
        stuck = s.execute(
            select(models.AgentTask).where(
                models.AgentTask.status.in_(
                    [state.TaskStatus.PENDING, state.TaskStatus.RUNNING]
                )
            )
        ).scalars().all()
        for task in stuck:
            was_running = task.status == state.TaskStatus.RUNNING
            old_status = task.status
            task.status = state.TaskStatus.INTERRUPTED
            task.finished_at = datetime.now()
            interrupted += 1
            logger.warning("agent_task=%s 崩溃恢复：%s → %s", task.id, old_status, task.status)
            # 仅 running 任务有在途岗位：已完成单位数 < 总单位数 时下一位在途
            if was_running and task.progress_done < task.progress_total:
                urls = list((task.params or {}).get("job_urls") or [])
                idx = task.progress_done or 0  # 0 基在途下标 = 已完成单位数
                if urls and 0 <= idx < len(urls):
                    url = urls[idx]
                    app = s.execute(
                        select(models.Application).where(models.Application.job_url == url)
                    ).scalar_one_or_none()
                    if app is not None:
                        if app.status in state.JobStatus.GREETABLE:
                            app.status = state.JobStatus.UNKNOWN
                            logger.warning(
                                "agent_task=%s 在途岗位 job_url=%s 发送结果未知 → status=unknown",
                                task.id, url,
                            )
                        unknown_jobs.append(
                            {
                                "job_url": url,
                                "job_title": app.job_title,
                                "company": app.company,
                                "application_id": app.id,
                                "task_id": task.id,
                            }
                        )
                    else:  # 岗位行已不存在（数据被清）：仅记录，无可隔离
                        unknown_jobs.append(
                            {
                                "job_url": url,
                                "job_title": None,
                                "company": None,
                                "application_id": None,
                                "task_id": task.id,
                            }
                        )
        s.commit()
        safe_pending = s.execute(
            select(func.count()).select_from(models.Application).where(
                models.Application.status.in_(sorted(state.JobStatus.GREETABLE))
            )
        ).scalar_one()
    if broadcast:
        broadcast(
            {
                "type": "agent_task_recovered",
                "interrupted": interrupted,
                "unknown": len(unknown_jobs),
                "safe_pending": safe_pending,
            }
        )
    return {
        "interrupted": interrupted,
        "unknown_jobs": unknown_jobs,
        "safe_pending": safe_pending,
    }


def resolve_unknown_result(
    engine,
    *,
    application_id: int,
    sent_confirm: bool,
    greeting: str | None = None,
) -> dict:
    """人工确认 unknown 岗位（结果未知 → 决定可不可重发）。

    - `sent_confirm=True`（招呼语确实发出）→ 置 `greeted` + 招呼语 + 时间戳；不再回库存，
      杜绝重复发送。
    - `sent_confirm=False`（未发出）→ 回 `pending`（进 GREETABLE，Agent 可安全续投）。

    仅对 `status=unknown` 生效；已确认/非 unknown 岗位返回 error（幂等门，防误清）。
    """
    with SASession(engine) as s:
        app = s.get(models.Application, application_id)
        if app is None:
            return {"error": "岗位不存在", "message": f"application_id={application_id} 不存在"}
        if app.status != state.JobStatus.UNKNOWN:
            return {
                "error": "非结果未知岗位",
                "message": f"application_id={application_id} 当前 status={app.status}，无需人工确认",
            }
        if sent_confirm:
            app.status = state.JobStatus.GREETED
            if greeting:
                app.greeting_text = greeting
            if app.greeting_sent_at is None:
                app.greeting_sent_at = datetime.now()
            result_status = state.JobStatus.GREETED
        else:
            app.status = state.JobStatus.PENDING  # 未发出 → 回库存可安全重发
            result_status = state.JobStatus.PENDING
        s.commit()
        logger.info("application_id=%s 结果未知已确认：sent=%s → status=%s", application_id, sent_confirm, result_status)
        return {
            "application_id": application_id,
            "job_url": app.job_url,
            "job_title": app.job_title,
            "sent_confirm": sent_confirm,
            "status": result_status,
        }
