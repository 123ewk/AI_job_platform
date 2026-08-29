"""agent/executor.py — 后台任务执行器骨架（SDD Step 4.1，§4.5 长任务后台执行）。

在既有功能下补一块"能后台跑、能看进度、能停"的扇骨，为 send_greetings（4.2）落地
做准备：

- **asyncio 执行器**：`TaskExecutor.submit()` 把 `self._run` 协程调度到执行器**自有的后台
  事件循环线程**（§4.2 起），驱动 `agent_tasks` 状态机 `pending → running → completed|failed|stopped`
  （§4.5，全部经 `state.can_transition` 合法路径）。执行器自带循环线程（懒启动 daemon），
  `submit` 用 `asyncio.run_coroutine_threadsafe` 交给它——**可从任意线程提交**，包括决策图
  graph 的 `asyncio.to_thread` worker 线程（那里没有运行中的事件循环，`create_task` 无法用），
  这正是 send_greetings（4.2）后台任务提交的执行路径；
- **进度事件**：每完成一个"岗位"（一个单位），`progress_done` 加一并广播一次
  `agent_task_progress`（复用 AgentHub 通道，spec §4.5 复用 broadcast_ws 思路）——
  对话里 Agent 据此能答"后台任务还剩 N 个"；
- **用户手动 stop**：`submit_stop(task_id)` 给任务打停止标志；执行器在**单位与单位之间**
  检查该标志（绝不打断正在跑的单位，避免"发送结果未知"状态），当前单位完整结束后任务
  进入 `stopped` 终态并广播 `agent_task_done`（spec §4.5，刹车柄只在用户手里）。

Step 4.1 是骨架：单位函数 `unit_fn` 由调用方注入，本步用假长任务（sleep 循环）验收；
真实 send_greetings 的浏览器单位在 Step 4.2 用 `_run_pw` 单线程池接入。中断恢复
（重启后 running→interrupted）与 API/dashboard 停止按钮属 Step 4.3/4.4。

线程模型：执行器本体是异步协程，跑在**自己专用的后台事件循环线程**（`_ensure_loop` 懒启动
daemon 线程 + `run_forever`），与调用方线程解耦——决策图在 `asyncio.to_thread` worker 线程、
无运行中循环也能 `submit`（`run_coroutine_threadsafe` 跨线程安全调度）；单位函数若为协程
直接 `await`，若为同步函数 `asyncio.to_thread` 丢线程池（与 pw 单线程池思想一致，不阻塞
事件循环——后台跑真浏览器操作的前提）。DB 状态写为本地 SQLite 微秒级操作，在循环线程内
直接执行。`submit_stop` 可能来自其它线程，用 `threading.Event` + 小锁保护，绝不在循环里
阻塞等待。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session as SASession

from agent.state import TaskStatus, can_transition
from db import base as db_base
from db import models

logger = logging.getLogger("agent.executor")

__all__ = ["TaskExecutor"]


class TaskExecutor:
    """后台长任务执行器骨架（§4.5）。

    `submit()` 建一条 `agent_tasks`（pending）+ 起后台协程跑完单位列表；每单位一次
    进度广播、终态（completed/failed/stopped）一次广播。`unit_fn` 为单岗位单位函数：
    协程函数直接 await，同步函数 to_thread。`broadcast` 是 WS 事件外发回调（缺省 no-op，
    经 `agent.api._get_executor` 接到 AgentHub）。
    """

    def __init__(
        self,
        *,
        engine=None,
        broadcast: Callable[[dict], Any] | None = None,
    ) -> None:
        self._engine = engine
        self._broadcast: Callable[[dict], Any] = broadcast or (lambda evt: None)
        # task_id -> 停止标志（threading.Event，submit_stop 跨线程翻转，锁保护防并发）
        self._stop: dict[int, threading.Event] = {}
        self._stop_lock = threading.Lock()
        # 执行器自有的后台事件循环线程（懒启动，§4.2 起可从无循环的 worker 线程 submit）
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        # task_id -> 运行中任务的句柄（`run_coroutine_threadsafe` 的 concurrent future，
        # 调用线程有循环时 `wrap_future` 成 asyncio.Future 供测试 join）
        self._tasks: dict[int, Any] = {}

    def _get_engine(self):
        return self._engine or db_base.get_engine()

    def submit(
        self,
        *,
        kind: str,
        total: int,
        unit_fn: Callable[[int], Any],
        params: dict | None = None,
        session_id: int | None = None,
    ) -> int:
        """建一条后台任务（status=pending）并起协程跑完，返回 `agent_tasks.id`。"""
        engine = self._get_engine()
        with SASession(engine) as s:
            row = models.AgentTask(
                session_id=session_id,
                kind=kind,
                params=params,
                status=TaskStatus.PENDING,
                progress_done=0,
                progress_total=max(total, 0),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            task_id = int(row.id)

        ev = threading.Event()
        with self._stop_lock:
            self._stop[task_id] = ev
        self._tasks[task_id] = self._schedule(task_id, kind=kind, total=total, unit_fn=unit_fn)
        return task_id

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """懒启动执行器后台事件循环线程（daemon，随进程终结）。

        执行器自持循环：submit 可从任意线程调度（`run_coroutine_threadsafe` 跨线程安全），
        不再依赖调用方线程有没有运行中的事件循环——graph worker（决策图在
        `asyncio.to_thread` 里）没有循环也能提交后台任务。
        """
        with self._loop_lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="agent-executor", daemon=True).start()
                self._loop = loop
        return self._loop

    def _schedule(self, task_id: int, *, kind: str, total: int, unit_fn: Callable) -> Any:
        """把 `_run` 协程调度起来，返回可 join 的句柄。

        条件双路径：
        - 调用线程**有**运行中的事件循环（测试的 `asyncio.run(scenario())`）→ 直接在该循环
          `create_task`，与 4.1 行为一致（任务跑在调用方循环线程，同步原语如 asyncio.Event
          均在同一条线程，无跨循环）；
        - 调用线程**无**循环（决策图 graph 的 `asyncio.to_thread` worker，send_greetings 提交
          后台任务的真实路径）→ 经 `run_coroutine_threadsafe` 调度到执行器自有后台循环线程，
          `wrap_future(loop=后台循环)` 返回该循环上的 asyncio.Future（本轮无人 join，`_run` 的
          finally 会 self._tasks.pop）。
        """
        try:
            caller = asyncio.get_running_loop()
        except RuntimeError:
            caller = None
        if caller is not None:
            return caller.create_task(self._run(task_id, kind=kind, total=total, unit_fn=unit_fn))
        loop = self._ensure_loop()
        return asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(self._run(task_id, kind=kind, total=total, unit_fn=unit_fn), loop),
            loop=loop,
        )

    def submit_stop(self, task_id: int) -> bool:
        """给任务打停止标志（§4.5 用户手动停止）。已知任务返回 True，未知返回 False。

        只在单位之间被检查——正在跑的单位完整结束后任务才进入 stopped 终态。
        线程安全（不进事件循环阻塞等待）。
        """
        with self._stop_lock:
            ev = self._stop.get(task_id)
            if ev is None:
                return False
            ev.set()
            return True

    async def _run(self, task_id: int, *, kind: str, total: int, unit_fn: Callable) -> None:
        engine = self._get_engine()
        # pending → running；非 pending 则放弃（例如已被别处抢跑）
        with SASession(engine) as s:
            row = s.get(models.AgentTask, task_id)
            if row is None or not can_transition(row.status, TaskStatus.RUNNING):
                return
            row.status = TaskStatus.RUNNING
            row.started_at = datetime.now()
            s.commit()

        status = TaskStatus.COMPLETED
        error: str | None = None
        done = 0
        try:
            for i in range(1, total + 1):
                # 单位之间检查停止标志：当前单位完整结束后才允许刹车
                if self._is_stop_requested(task_id):
                    status = TaskStatus.STOPPED
                    break
                if inspect.iscoroutinefunction(unit_fn):
                    await unit_fn(i)
                else:
                    await asyncio.to_thread(unit_fn, i)
                done = i
                self._persist_and_broadcast_progress(engine, task_id, kind, total, done)
        except Exception as exc:  # noqa: BLE001 —— 单岗位失败记 status=failed，不打死执行器
            status = TaskStatus.FAILED
            error = str(exc)
            logger.exception("agent_task=%s 单位执行失败", task_id)
        finally:
            with SASession(engine) as s:
                row = s.get(models.AgentTask, task_id)
                if row is not None and can_transition(row.status, status):
                    row.status = status
                    row.finished_at = datetime.now()
                    if error is not None:
                        row.error = error
                    s.commit()
            self._broadcast(
                {
                    "type": "agent_task_done",
                    "task_id": task_id,
                    "kind": kind,
                    "status": status,
                    "done": done,
                    "total": total,
                    "error": error,
                }
            )
            with self._stop_lock:
                self._stop.pop(task_id, None)
            self._tasks.pop(task_id, None)

    def _is_stop_requested(self, task_id: int) -> bool:
        ev = self._stop.get(task_id)
        return ev is not None and ev.is_set()

    def _persist_and_broadcast_progress(
        self, engine, task_id: int, kind: str, total: int, done: int
    ) -> None:
        with SASession(engine) as s:
            row = s.get(models.AgentTask, task_id)
            if row is not None:
                row.progress_done = done
                s.commit()
        self._broadcast(
            {
                "type": "agent_task_progress",
                "task_id": task_id,
                "kind": kind,
                "done": done,
                "total": total,
            }
        )
