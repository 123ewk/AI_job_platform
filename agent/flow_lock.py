"""agent/flow_lock.py — 浏览器互斥锁 FlowLock（SDD Step 3.3 / spec §4.6）。

存量只靠 pw 单线程池保证浏览器"不并发"，但表达不了"谁在用、让给谁"：
`monitor_paused` 布尔只管用户暂停，`browser_sync_lock`（asyncio.Lock）只管会话同步。
FlowLock 升级二者为**显式互斥**——带 owner 标签、支持阻塞获取（排队）与非阻塞查询
（跳过本轮），是 Agent 浏览器类工具与 HR 监控轮询共享的互斥通道：

- Agent 浏览器类工具（search_jobs，Phase 4.2 起 send_greetings 后台任务）执行期间
  **持有**锁（owner="agent:search_jobs"）——`chat_monitor_loop` 每轮非阻塞
  `flow_lock.locked()` 查询，被占则跳过本轮（§4.6 "持有期间跳过本轮"）；
- 监控循环/同步流程持有期间，Agent 工具 `acquire(owner, blocking=True)` **排队等待**
  而不是报错（§4.6 "排队而非并发"，Step 3.3 验收测试焦点）；
- `monitor_paused` 的读写点在 boss_app 换成 FlowLock 查询（等价替换，行为不变）。

线程模型：工具在 `asyncio.to_thread` 工作线程执行、监控循环在事件循环线程执行——
用 `threading.Lock` 而非 asyncio.Lock：两处都能用，且 asyncio 侧只做非阻塞查询，
不会阻塞事件循环。`release()` 幂等（未持有调用为 no-op），等价替换
`monitor_paused = was_paused` 的恢复语义，避免 finally 双释放炸 RuntimeError。
"""

from __future__ import annotations

import threading

__all__ = ["FlowLock", "default_flow_lock"]


class FlowLock:
    """带 owner 标签的浏览器互斥锁（§4.6）。

    `owner` 是当前持有者标签（"agent:search_jobs:python" / "sync" / "search" ...），
    供日志/状态展示；互斥权威是底层 `threading.Lock`（跨线程安全）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        """当前持有者标签（仅展示用；读取为尽力而为，互斥状态以 `locked()` 为准）。"""
        return self._owner

    def acquire(self, owner: str, *, blocking: bool = True, timeout: float | None = None) -> bool:
        """获取锁。

        - `blocking=True`（默认）：排队等待，直到拿到锁——Agent 工具用（§4.6 排队语义）；
        - `blocking=False`：立即返回是否抢到——非阻塞查询场景用。
        成功时记录持有者标签；返回 False 表示未拿到（被占 / 超时）。
        """
        # threading.Lock.acquire(timeout=None) 报 TypeError——None 转"无限等待"：
        # blocking=True 且 timeout=None → 不传 timeout（默认 -1 永久等待，§4.6 排队语义）。
        if timeout is None:
            got = self._lock.acquire(blocking=blocking)
        else:
            got = self._lock.acquire(blocking=blocking, timeout=timeout)
        if got:
            self._owner = owner
        return got

    def release(self) -> None:
        """释放锁。幂等：未持有调用为 no-op（等价替换 monitor_paused 恢复语义）。"""
        if not self._lock.locked():
            return
        self._lock.release()
        self._owner = None

    def locked(self) -> bool:
        """是否被占用（非阻塞）。`chat_monitor_loop` 每轮据此决定是否跳过本轮。"""
        return self._lock.locked()

    def __enter__(self) -> "FlowLock":
        self.acquire("ctx")
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


# 模块级单例：boss_app 的 monitor 循环与 agent 工具共享同一把锁（§4.6）。
# 测试注入独立锁实例，避免污染该单例。
default_flow_lock = FlowLock()
