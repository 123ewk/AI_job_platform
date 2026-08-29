"""agent/state.py — Agent 状态机常量（SDD Step 2.1）。

Agent 各行军状态域的**单一真源**：运行时代码（决策图 / 后台执行器 / 审批门）
变更 status / kind / execution_mode 一律从这里取值，不散落魔法字符串。

对应 4 张 Agent 表（db/models.py，Step 1.1 已建模）的列默认值，最终校验由
tests/test_agent_state.py 用 `测试常量 == 模型默认值` 钉死，任一侧漂移即红。

覆盖 6 个状态域：
- `ExecutionMode`  : AgentSession.execution_mode（§4.3：audit 审计默认 / autonomous 全权）
- `SessionStatus`  : AgentSession.status 生命周期
- `TaskStatus`     : AgentTask.status（§4.5 后台长任务状态机，含合法转换图）
- `ApprovalStatus` : Approval.status（§4.3 审批：pending/approved/rejected）
- `StepStatus`     : AgentStep.status（transcript 步骤成败）
- `StepKind`       : AgentStep.kind（步骤类型，非状态机，供回放归类）

注意：DB 列本身无 CHECK 约束（见 1.1 迁移），合法性由本模块常量 + 转换规则在
应用层把关；`TaskStatus.TRANSITIONS` / `can_transition` / `is_terminal` 即提议的
唯一转换通道，后台执行器（Phase 4）据此驱动。
"""

from __future__ import annotations

from typing import ClassVar, Final

__all__ = [
    "ExecutionMode",
    "SessionStatus",
    "TaskStatus",
    "ApprovalStatus",
    "StepStatus",
    "StepKind",
    "can_transition",
    "is_terminal",
]


class ExecutionMode:
    """两种执行模式（§4.3）。默认 audit（审计：每个工具调用前挂起等确认）。"""

    AUDIT: Final[str] = "audit"
    AUTONOMOUS: Final[str] = "autonomous"
    ALL: ClassVar[frozenset[str]] = frozenset({AUDIT, AUTONOMOUS})


class SessionStatus:
    """一次 Agent 对话会话的生命周期。"""

    ACTIVE: Final[str] = "active"
    COMPLETED: Final[str] = "completed"
    ABORTED: Final[str] = "aborted"
    ALL: ClassVar[frozenset[str]] = frozenset({ACTIVE, COMPLETED, ABORTED})


class TaskStatus:
    """后台长任务状态机（§4.5）：pending → running → 终态。

    终态四种：completed / failed / interrupted / stopped。interrupted 用于进程
    崩溃恢复（重启后 running 任务标 interrupted）；续投由 Agent 提议新建任务完成，
    任务本体不再复活——interrupted 本身是终态。
    """

    PENDING: Final[str] = "pending"
    RUNNING: Final[str] = "running"
    COMPLETED: Final[str] = "completed"
    FAILED: Final[str] = "failed"
    INTERRUPTED: Final[str] = "interrupted"
    STOPPED: Final[str] = "stopped"
    ALL: ClassVar[frozenset[str]] = frozenset({PENDING, RUNNING, COMPLETED, FAILED, INTERRUPTED, STOPPED})

    # 合法转换图：非终态都要有可到达目标；终态目标为空集 → is_terminal 判定。
    TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        PENDING: frozenset({RUNNING}),
        RUNNING: frozenset({COMPLETED, FAILED, INTERRUPTED, STOPPED}),
        COMPLETED: frozenset(),
        FAILED: frozenset(),
        INTERRUPTED: frozenset(),
        STOPPED: frozenset(),
    }


class ApprovalStatus:
    """审批状态（§4.3）。pending → approved / rejected；rejected ≠ 终止会话，
    仅拒绝该次工具调用（拒绝结果回灌 LLM 决定改道/收尾）。"""

    PENDING: Final[str] = "pending"
    APPROVED: Final[str] = "approved"
    REJECTED: Final[str] = "rejected"
    ALL: ClassVar[frozenset[str]] = frozenset({PENDING, APPROVED, REJECTED})


class StepStatus:
    """transcript 步骤的成败状态。"""

    DONE: Final[str] = "done"
    FAILED: Final[str] = "failed"
    ALL: ClassVar[frozenset[str]] = frozenset({DONE, FAILED})


class StepKind:
    """transcript 步骤类型（§4.1/§4.3）；非状态机，供回放归类。"""

    PLAN: Final[str] = "plan"
    EXECUTE: Final[str] = "execute"
    APPROVAL: Final[str] = "approval"
    REPORT: Final[str] = "report"
    ASK_USER: Final[str] = "ask_user"
    ALL: ClassVar[frozenset[str]] = frozenset({PLAN, EXECUTE, APPROVAL, REPORT, ASK_USER})


def can_transition(current: str, target: str) -> bool:
    """后台任务 status 是否允许 current → target（§4.5）。未知状态视为不可迁移。"""
    return target in TaskStatus.TRANSITIONS.get(current, frozenset())


def is_terminal(task_status: str) -> bool:
    """后台任务是否已达终态（无可再迁）。未知状态按非终态对待。"""
    return not TaskStatus.TRANSITIONS.get(task_status, frozenset())
