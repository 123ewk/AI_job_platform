# 更新日志

## v4.1 — 合并 PR #3 + AI 智能体后端补完 (2026-06-22)

合并上游 PR #3 (v4.0 from xun-x33) 后，补回 main 端历史 commit 527a6bc 引入的 3 个 AI 端点。

### ✨ 新增端点

- `POST /api/jobs/optimize-resume` — AI 简历优化（24h DB 缓存，支持 `force_refresh` 强制重新生成）
- `POST /api/jobs/chat-suggestion` — AI 沟通建议（24h DB 缓存，根据 HR 姓名/头衔/法人状态生成话术）
- `POST /api/jobs/analyze` — 输出补 `decision` / `reasons` / `risks` / `suggested_questions` 4 个字段

### 🗄️ 数据库迁移

`applications` 表新增 4 列（启动时自动 ALTER）：
- `optimize_result TEXT` / `optimize_at TIMESTAMP`
- `chat_suggestion_result TEXT` / `chat_suggestion_at TIMESTAMP`

### 📁 保留的 main 端独有文件

- `boss_company.py` — 公司画像聚合（被 `tests/test_smart_send.py` 引用）
- `boss_geo.py` — BOSS 城市/区/规模映射（待前端接入）
- `interview/config.py` — interview 子模块配置
- `start.bat` — Windows 一键启动

### ⚠ 已知未补回（PR 端用新方式实现或丢失）

- 简历优化 + 沟通建议的**前端弹窗 UI**：dashboard.html 被 PR 端整体重写，旧 UI 入口需重做
- HR 真实姓名/法人识别/法人直聘 tag：PR 端搜索流程已实现部分能力（前端字段名不同，未对齐）
- `send_resume` 按钮 `innerText` 兜底：PR 端使用不同的 selector 策略
- CLI 端的 `analyze` / 候选池增强：未对齐到 lakejob CLI

---

## v4.0 — 聊天页深度优化 + 搜索体验升级 (2026-06-16)

**128 commits · 2026-05-15 ~ 2026-06-16**

从首次 fork 至今，项目经历了从基础岗位采集工具到全功能 Web 控制台的全面进化。以下是按功能模块整理的完整更新记录。

---

### 🏗️ 架构 & 后端

- **Playwright 替代 Firefox 方案**：从 Selenium/Firefox 切换到 Playwright，反爬稳定性大幅提升
- **FastAPI Web 控制台**：REST API + WebSocket 实时推送，支持远程管理
- **SQLite WAL 模式**：会话/消息/岗位/设置全量持久化，支持并发读写
- **一键启动脚本** `start.bat`：检查依赖 → 启动浏览器 → 打开控制台，零配置
- **全局 print 时间戳**：monkey-patch `builtins.print`，所有日志自动加 `[HH:MM:SS]`
- **CLI 12 条命令**：search / apply / scan / stats / shortlist 等，支持 Agent 集成

### 💬 聊天页 (Chat)

- **聊天框同步遮罩**：点击会话时显示「正在对齐颗粒度...📡」模糊遮罩，等 sync 返回最新数据后才渲染，避免误看旧缓存
- **岗位信息精确提取**：从 BOSS DOM 的 `.position-name` / `.salary` / `.city` 精确提取岗位名、薪资、城市，替换之前猜测 class 名的方案
- **会话列表红点修复**：修复 `unread_delta` 硬编码为 0 导致红点不显示的问题
- **会话列表岗位名显示**：HR 名字右边显示 `岗位名 · 薪资 · 城市`
- **聊天头部去重**：移除重复人名和「更多」噪音，只显示岗位名+薪资+城市
- **在线状态实时更新**：对方离线后及时清空 `online_status`，不再永远显示「在线」
- **sync 接口 key 修复**：`boss_app.py` 的 sync 端点从旧的 `company/title` key 更新为 `jobTitle/salary/city`
- **清空聊天数据**：一键清空所有会话和消息记录

### 🔍 岗位搜索 (Search)

- **搜索筛选常驻显示**：薪资/学历/经验/规模/类型/区域全部可见，无需展开
- **区域筛选优化**：选中 >2 个区域时只显示前 2 个 + 省略号（如 `天河+白云... (5)`），容器随页面缩放自适应
- **搜索行自动换行**：`flex-wrap:wrap`，缩放时筛选框自动跳行不溢出
- **岗位名称黑名单过滤**：用户自定义过滤关键词，命中的岗位入库为 `filtered` 状态
- **搜索过滤统计**：搜索完成后分类展示过滤原因（薪资/学历/经验/规模/类型/福利/公司去重/HR不活跃）
- **福利标签筛选**：支持按福利关键词（如五险一金、双休）过滤
- **智能等待**：搜索从固定 `pause(3,5)` 改为智能等待，省时 10-30s
- **持续滚动**：`_scroll_all` 持续滚动直到无新内容加载，修复搜索只返回部分结果
- **搜索词/城市记忆**：localStorage 记住上次输入，刷新后自动恢复
- **岗位列表上限 1000 条**：超出自动淘汰最旧的 pending 记录

### 📊 投递记录 (Applications)

- **投递漏斗统计**：搜索页 funnel 卡片 + 投递记录页 stat 卡片，数据同源
- **HR回复/面试统计**：从投递记录页移到聊天页，统计逻辑统一为 24h
- **分页支持**：投递记录页分页，每页显示数量可调
- **本地候选池**：shortlists 表 + API + 前端收藏按钮 + CLI shortlist 命令

### 🤖 AI & 自动回复

- **智能招呼语**：真正调用 LLM 生成个性化招呼语（之前是空壳），失败自动回退到用户自定义模板
- **AI JD 分析**：POST /api/jobs/analyze + 前端弹窗，无简历时自动切换为 JD 分析模式
- **批量投递优化**：只调一次 LLM（之前每条都等），性能提升显著
- **deepseek-v4-pro 支持**：AI 配置新增模型选项

### 🎨 UI/UX

- **深色科技风 → Apple 极简风 → Linear 风格**：经历 4 次 UI 重设计，最终定稿为深色科技风 + 左侧边栏布局
- **统计卡片自适应**：5 个卡片同排，自动适配不同宽度
- **保存设置即时反馈**：按钮变 ✓ 已保存 (1.5s) / ✗ 保存失败
- **设置页重组**：HR不活跃/公司去重移到搜索页统一管理

### 🔒 安全 & 清理

- **移除硬编码密码**：interview/ 数据库配置改读环境变量
- **清理历史遗留**：删除 scraper.py、config.yaml、旧 CSS 残留
- **no-cache 头**：dashboard.html 响应加 `no-cache`，防止浏览器缓存旧版
- **.gitignore 完善**：排除 reports/.claude 等追踪文件

### 🐛 关键 Bug 修复

- 修复岗位类型 code 错误（1901/1903 而非 1/2）导致筛选不生效
- 修复搜索乱码 + 筛选条件全不生效
- 修复兼职/乱码岗位入库（title 黑名单 + 乱码兜底）
- 修复 `_run_pw` 不支持 kwargs 导致 search 7 个新字段被丢弃
- 修复「收到回复」计数虚增的两个根因
- 修复 BOSS 中央弹窗聊天框 selector 兜底
- 修复 send_resume 适配 BOSS 新弹窗

---

> 📝 完整 commit 历史：128 commits · `e5eff28` ~ `caf4b14`
