# 更新日志

本项目 fork 自开源项目 [lakejobai-job-radar](https://github.com/lake121380-source/lakejobai-job-radar)（2026-08-28 baseline 整体导入），此后在本仓库独立演进。以下为本仓库真实提交历史（46 commits · 2026-08-28 ~ 2026-08-31，单一维护者）。

## v1.3.0 — 岗位筛选/勾选投递 + Agent 浏览器线程守卫 (2026-08-30 ~ 08-31)

- **岗位列表**：岗位关键词筛选、勾选投递/批量删除、审批卡 min-width 定妆
- **投递记录**：同款关键词过滤 + 勾选投递/删除；放行规则先放宽（只拦 `applied`）再落终版（已过滤同拦 + 静默跳过）
- **dashboard**：批量投递撞验证码立即中止 + 失败原因上屏 + 状态徽标补全；修复启动行 TDZ 中断导致的岗位列表/Agent 面板失效
- **简历发送确认链路实测校准**：确认键 `.panel-resume .btn-sure-v2` + unable 预检 + 未确认不再记成功
- **Agent 浏览器工具链**：浏览器生命周期工具 + 执行前预检 + 异常文案友好化
- **pw 线程身份守卫**：`start` 盖线程戳 + `heartbeat`/`close` 拦截 + `open_browser` 拒绝跨线程重建
- **杂项**：gitignore 排除私有面试题库 `docs/INTERVIEW_QUESTIONS.md`；README 同步 V1.3.0

## v1.2.x — Agent 化改造 (2026-08-28 ~ 2026-08-30)

按 SDD 规格（docs/AI_job_platform_Agent化改造SDD_V1.0.md）分步推进：

- **Step 1 数据层工程化**：SQLAlchemy 2.0 models + Alembic（11 表）、`DB_BACKEND` 开关薄转发层 `db/backend.py`、`boss_state_sa` 适配层、`migrate_legacy` 幂等迁移 CLI
- **Step 2 LLM + 决策图**：`llm_client` function-calling 扩展 + Pydantic 工具 schema、LangGraph `StateGraph` + `SqliteSaver` checkpoint + transcript 落库、对话 API `POST /api/agent/chat` + WS `/ws/agent`
- **Step 3 工具链**：`query_jobs` / `update_setting` / `search_jobs`（浏览器工具）+ FlowLock 互斥 + JobStatus 状态机映射（白名单/敏感键硬拒/脱敏落 graph）
- **Step 4 后台执行**：TaskExecutor 骨架 + `agent_tasks` 状态机 + 进度/stop 广播、`send_greetings` 接入、崩溃恢复 + 结果未知岗位人工确认门、审批门 + 手动停止端点 `/api/agent/tasks/{id}/stop` + 连续失败熔断联动
- **Step 5 安全与演练**：注入防御链 L0-L5（分隔符包裹 + untrusted 检测告警 + 输出过滤）、全局 `dry_run` 演练开关
- **Step 6 LLM planner + 对话面板**：真 LLM planner 接入决策图 + DeepSeek 思考模式关闭、dashboard 🤖 Agent 对话面板（对话/审批卡片/WS 步骤流/后台任务卡片）、同 thread 多轮输入被吞等 hotfix（V1.2.26 ~ V1.2.28）
- **文档**：README Agent 章节、TECHNICAL_ANALYSIS FlowLock 并发模型、docs/AGENT_USAGE.md、SDD spec 补全

## v1.0 — baseline 导入 (2026-08-28)

- fork 自 `lakejobai-job-radar`，整体导入既有采集/投递/聊天/AI 能力
- 外部化 `AI_API_KEY` 到 `.env`（env-first，settings 兜底）
- structured JSON log baseline + 脱敏掩码（agent/log_config.py）
- 声明 interview 运行时依赖（numpy、pymysql）；排除本地原仓库 git 历史备份
