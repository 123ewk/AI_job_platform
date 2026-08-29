"""SDD Step 2.1：Agent 4 张表的状态机常量验收（红→绿，先红）。

本文件先存在（红，`agent/state.py` 尚未实现），实现后绿。覆盖四条线：

1. **常量 == 模型默认值**：`agent.state` 的 5 个状态域常量是单一真源，必须与
   `db/models.py` 的列默认值逐一对齐（任一侧漂移即红——门禁声明了 schema 语义）。
2. **agent_tasks 状态机合法**（§4.5：pending→running→completed|failed|interrupted|stopped）：
   每个状态要么能迁、要么是终态；终态不可再迁、不可回滚；转换目标都在常量集内。
3. **声明集完备**：每个状态域 `ALL` 不含空项、无重复，且与模型列默认/注释口径一致。
4. **全生命周期读写**：内存 SQLite 建 4 张 agent 表 → 插入会话/任务/步骤/审批 →
   按转换图迁移任务状态 → 读回校验（验证 models 表述与常量配合可用）。

注：4 张表已由 Step 1.1 建模并进入 alembic 初始迁移 `9f808e900204`
（模型+DLL 已在 1.1 落地并冒烟验证），故本步增量 = 状态机常量 + 整件对齐。
"""

from __future__ import annotations

from sqlalchemy import create_engine

from agent import state
from db import models

# ──────────────────────────────────────────────────────────
#  验收 1：常量与模型默认值逐一对齐
# ──────────────────────────────────────────────────────────


def test_execution_mode_audit_is_model_default():
    # §4.3：默认建议 audit（审计）。session 列的 Python 默认值必须锚定该常量。
    assert state.ExecutionMode.AUDIT == "audit"
    col_default = models.AgentSession.__table__.c.execution_mode.default
    assert col_default.arg == state.ExecutionMode.AUDIT


def test_session_status_active_is_model_default():
    col_default = models.AgentSession.__table__.c.status.default
    assert col_default.arg == state.SessionStatus.ACTIVE


def test_task_status_pending_is_model_default():
    col_default = models.AgentTask.__table__.c.status.default
    assert col_default.arg == state.TaskStatus.PENDING


def test_approval_status_pending_is_model_default():
    col_default = models.Approval.__table__.c.status.default
    assert col_default.arg == state.ApprovalStatus.PENDING


# ──────────────────────────────────────────────────────────
#  验收 2：agent_tasks 状态机转换合法（§4.5）
# ──────────────────────────────────────────────────────────


def test_task_transition_and_terminal_rules():
    # 合法前进
    assert state.can_transition(state.TaskStatus.PENDING, state.TaskStatus.RUNNING)
    assert state.can_transition(state.TaskStatus.RUNNING, state.TaskStatus.COMPLETED)
    assert state.can_transition(state.TaskStatus.RUNNING, state.TaskStatus.FAILED)
    assert state.can_transition(state.TaskStatus.RUNNING, state.TaskStatus.INTERRUPTED)
    assert state.can_transition(state.TaskStatus.RUNNING, state.TaskStatus.STOPPED)
    # 不允许：回滚 / 跳过 running / 从终态继续
    assert not state.can_transition(state.TaskStatus.RUNNING, state.TaskStatus.PENDING)
    assert not state.can_transition(state.TaskStatus.PENDING, state.TaskStatus.COMPLETED)
    assert not state.can_transition(state.TaskStatus.COMPLETED, state.TaskStatus.RUNNING)
    assert not state.can_transition(state.TaskStatus.STOPPED, state.TaskStatus.RUNNING)
    # 终态判别（§4.5：completed|failed|interrupted|stopped 皆终态）
    for t in (state.TaskStatus.COMPLETED, state.TaskStatus.FAILED,
              state.TaskStatus.INTERRUPTED, state.TaskStatus.STOPPED):
        assert state.is_terminal(t)
    assert not state.is_terminal(state.TaskStatus.PENDING)
    assert not state.is_terminal(state.TaskStatus.RUNNING)


def test_task_status_set_is_well_formed():
    # 每个状态的转换目标都落在全集内；无可到达源即终态（与 is_terminal 一致）。
    assert state.TaskStatus.ALL == {
        "pending", "running", "completed", "failed", "interrupted", "stopped",
    }
    for cur, targets in state.TaskStatus.TRANSITIONS.items():
        assert cur in state.TaskStatus.ALL
        assert targets <= state.TaskStatus.ALL
        assert state.is_terminal(cur) == (not targets)


# ──────────────────────────────────────────────────────────
#  验收 3：声明集完备、无空/重复
# ──────────────────────────────────────────────────────────


def test_state_domains_are_nonempty_and_distinct():
    domains = [
        state.ExecutionMode.ALL,
        state.SessionStatus.ALL,
        state.TaskStatus.ALL,
        state.ApprovalStatus.ALL,
        state.StepStatus.ALL,
        state.StepKind.ALL,
    ]
    for d in domains:
        assert d, "状态域常量集不可为空"
        assert len(d) == len(set(d)), "状态域常量集不可有重复"


# ──────────────────────────────────────────────────────────
#  验收 4：4 张 agent 表全生命周期读写（内存 SQLite）
# ──────────────────────────────────────────────────────────


def test_agent_tables_lifecycle_roundtrip():
    eng = create_engine("sqlite://")
    models.Base.metadata.create_all(eng)

    with eng.begin() as conn:
        # 会话：默认审计模式 → 显式全权
        conn.execute(
            models.AgentSession.__table__.insert().values(
                graph_thread_id="thr-1", execution_mode=state.ExecutionMode.AUTONOMOUS,
            )
        )
        sess_id = conn.exec_driver_sql(
            "SELECT id FROM agent_sessions WHERE graph_thread_id='thr-1'"
        ).scalar()

        # 任务：按转换图迁移 pending→running→stopped
        conn.execute(
            models.AgentTask.__table__.insert().values(
                session_id=sess_id, kind="send_greetings",
                status=state.TaskStatus.PENDING,
            )
        )
        task_id = conn.exec_driver_sql(
            "SELECT id FROM agent_tasks WHERE session_id=?", (sess_id,)
        ).scalar()
        conn.execute(
            models.AgentTask.__table__.update().where(models.AgentTask.id == task_id).values(
                status=state.TaskStatus.RUNNING
            )
        )
        conn.execute(
            models.AgentTask.__table__.update().where(models.AgentTask.id == task_id).values(
                status=state.TaskStatus.STOPPED
            )
        )

        # 步骤 transcript + 审批记录
        conn.execute(
            models.AgentStep.__table__.insert().values(
                session_id=sess_id, kind=state.StepKind.EXECUTE, tool_name="send_greetings",
                status=state.StepStatus.DONE,
            )
        )
        conn.execute(
            models.Approval.__table__.insert().values(
                session_id=sess_id, task_id=task_id, tool_name="send_greetings",
                status=state.ApprovalStatus.APPROVED,
            )
        )

    # 读回校验
    with eng.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT execution_mode FROM agent_sessions WHERE id=?", (sess_id,)
        ).scalar() == state.ExecutionMode.AUTONOMOUS
        assert conn.exec_driver_sql(
            "SELECT status FROM agent_tasks WHERE id=?", (task_id,)
        ).scalar() == state.TaskStatus.STOPPED
        assert conn.exec_driver_sql(
            "SELECT COUNT(*) FROM agent_steps WHERE session_id=?", (sess_id,)
        ).scalar() == 1
        assert conn.exec_driver_sql(
            "SELECT status FROM approvals WHERE task_id=?", (task_id,)
        ).scalar() == state.ApprovalStatus.APPROVED
