"""agent/state.py — Agent 状态机常量 + 配置边界 + 脱敏（SDD Step 2.1 / 3.2）。

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
- `JobStatus`      : applications.status 的 Agent 岗位状态机（§4.2 工具词汇，Step 3.1 引入，
  含 `ungreeted` 过滤所需的 GREETABLE 集合；**不另立列**，直接读写现有 applications.status）

Step 3.2 追加两类安全常量：
- `SETTINGS_WHITELIST`：update_setting 可写配置白名单（== 手动设置 API `boss_app.SettingsUpdate`
  字段集，测试钉死对齐；Agent 写配置能力上界 = 人工 API 允许的上界）
- `SENSITIVE_SETTING_KEYS`：敏感设置键（ai_api_key/wechat_id，spec §4.2 明示）——update_setting
  **全模式硬拒**，唯一可写路径是人工 `/api/settings`；配合 `mask_sensitive` 在日志/transcript 脱敏。

注意：DB 列本身无 CHECK 约束（见 1.1 迁移），合法性由本模块常量 + 转换规则在
应用层把关；`TaskStatus.TRANSITIONS` / `can_transition` / `is_terminal` 即提议的
唯一转换通道，后台执行器（Phase 4）据此驱动。
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from agent.log_config import mask_value as _mask_log_value

__all__ = [
    "ExecutionMode",
    "SessionStatus",
    "TaskStatus",
    "ApprovalStatus",
    "StepStatus",
    "StepKind",
    "JobStatus",
    "SETTINGS_WHITELIST",
    "SENSITIVE_SETTING_KEYS",
    "mask_sensitive",
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


class JobStatus:
    """Agent 岗位状态机（§4.2 query_jobs/search_jobs 工具词汇）→ 现有 `applications.status` 列。

    不另立平行列——Agent 直接读写既有 `applications.status`，与 dashboard 去重口径
    （boss_state.applied_status）共享同一列：

    - `DISCOVERED` : search_jobs 新入库（Phase 3.3 写 applications.status='discovered'）
    - `PENDING`    : 存量默认状态（既有 apply_batch 的待投递库存口径）
    - `GREETED`    : send_greetings 打招呼成功后（Phase 4.2 写 'greeted'）
    - `APPLIED` / `REPLIED` / `INTERVIEW` : 存量"已进入投递/对话"状态
    - `FILTERED`   : 投递时被关键词过滤（不可再打招呼）

    `GREETABLE` = query_jobs(ungreeted=true) 的过滤集合：存量 pending + Agent 新入库
    discovered。filtered 被排除（已按关键词过滤，不得再打招呼）；PROGRESSED 是
    "已打过招呼/已投递对话"集合，与 GREETABLE 不相交。
    """

    DISCOVERED: Final[str] = "discovered"
    PENDING: Final[str] = "pending"
    GREETED: Final[str] = "greeted"
    APPLIED: Final[str] = "applied"
    REPLIED: Final[str] = "replied"
    INTERVIEW: Final[str] = "interview"
    FILTERED: Final[str] = "filtered"
    ALL: ClassVar[frozenset[str]] = frozenset(
        {DISCOVERED, PENDING, GREETED, APPLIED, REPLIED, INTERVIEW, FILTERED}
    )
    # 已打过招呼 / 已进入投递或对话 → 不可再打招呼
    PROGRESSED: ClassVar[frozenset[str]] = frozenset({GREETED, APPLIED, REPLIED, INTERVIEW})
    # 可打招呼库存（query_jobs ungreeted=true 的语义来源）
    GREETABLE: ClassVar[frozenset[str]] = frozenset({PENDING, DISCOVERED})


# ══════════════════════════════════════════════════════════
#  update_setting 配置边界 + 脱敏（Step 3.2，spec §3.2/§4.2/§4.3）
# ══════════════════════════════════════════════════════════

# update_setting 可写配置白名单。单一真源 = 手动设置 API 的 `boss_app.SettingsUpdate` 字段集
# （§4.2"复用同一白名单"；Agent 写配置的上界 == 人工 API 允许的上界）。agent 侧独立定义，
# 避免反向 import boss_app（boss_app 顶层 import agent.api → 循环）。漂移由
# tests/test_agent_tools.py::test_whitelist_aligns_with_manual_settings_api 钉死。
SETTINGS_WHITELIST: Final[frozenset[str]] = frozenset({
    "greeting_template", "greeting_mode", "smart_greeting_prompt", "title_filter_keywords",
    "greeting_enabled", "ai_reply_style", "daily_apply_limit", "auto_reply_enabled",
    "min_reply_delay_sec", "max_reply_delay_sec", "batch_delay_min_sec", "batch_delay_max_sec",
    "resume_summary", "wechat_id", "search_keywords", "default_city", "selector_overrides",
    "ai_api_key", "ai_base_url", "ai_model", "ai_platform", "user_location",
    "conversation_cooldown_sec", "reply_rules_system_prompt", "filter_inactive_hr",
    "dedup_company_by_default", "max_hr_inactive_days",
})

# 敏感设置键：update_setting 一律拒绝（全模式，autonomous 也不放过）。§4.2 明示 api_key、
# wechat_id；§3.2"强制审计模式"取最严解释——Agent 无路径改敏感键，唯一可写路径是
# 人工 `/api/settings`。is subset of SETTINGS_WHITELIST（白名单通过、再被敏感检查拦截）。
SENSITIVE_SETTING_KEYS: Final[frozenset[str]] = frozenset({"ai_api_key", "wechat_id"})

_MASK = "***"

# 键名含以下片段 → 值掩码（键名保留，便于审计定位是哪一字段泄露）
_SENSITIVE_KEY_HINTS = (
    "api_key", "apikey", "secret", "token", "password", "passwd",
    "authorization", "wechat", "phone", "mobile",
)


def _is_sensitive_key_name(name: str) -> bool:
    return any(h in name.lower() for h in _SENSITIVE_KEY_HINTS)


def mask_sensitive(value: Any) -> Any:
    """递归脱敏：api_key/wechat_id/手机号在日志与 transcript 一律掩码（spec §4.3）。

    复用 Step 0.3 的 `agent.log_config.mask_value`（文本级单真源：手机号 `138****8000`、
    sk-/Bearer token 保留首尾、wechat 键值全掩），其上补两层**结构化**规则（log_config
    无法覆盖的字段形状）：

    - dict 形如 `{"key": "ai_api_key", "value": "sk-..."}`：key 命中敏感设置键 → value **全掩**
      （update_setting 的入参形状；value 才是秘密，key 名保留便于定位）——不依赖 value 文本
      形状，短值如 `sk-abc` 也拦得住；
    - 其他 dict：键名含敏感提示（api_key/secret/token/wechat/phone…）→ 该值全掩。

    其余 str/list/tuple 委托 `log_config.mask_value`。幂等（已掩码输入再跑结果不变）。
    """
    if isinstance(value, dict):
        key_field = value.get("key") or value.get("name")
        if isinstance(key_field, str) and key_field in SENSITIVE_SETTING_KEYS and "value" in value:
            return {**value, "value": _MASK}
        return {
            k: (_MASK if _is_sensitive_key_name(k) else mask_sensitive(v))
            for k, v in value.items()
        }
    return _mask_log_value(value)


def can_transition(current: str, target: str) -> bool:
    """后台任务 status 是否允许 current → target（§4.5）。未知状态视为不可迁移。"""
    return target in TaskStatus.TRANSITIONS.get(current, frozenset())


def is_terminal(task_status: str) -> bool:
    """后台任务是否已达终态（无可再迁）。未知状态按非终态对待。"""
    return not TaskStatus.TRANSITIONS.get(task_status, frozenset())
