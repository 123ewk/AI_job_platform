# 项目优化变更说明

> 优化基于 `lakejobai-job-radar` 原项目（https://github.com/lake121380-source/lakejobai-job-radar）

## 修改的核心问题

| # | 原问题 | 优化方案 |
|---|--------|----------|
| 1 | 已发过的公司信息会重复发送 | 按公司名去重（支持中缀/后缀变体），自动跳过 |
| 2 | 一键投递只能扫当前页 | 新增翻页扫描 + 多页批量投递（默认 5 页） |
| 3 | 岗位分析缺少公司信息 | 分析时自动抓取公司：行业/规模/成立时间/在招岗位等 |
| 4 | HR 长时间不上线也投递（浪费每日上限） | 新增 HR 活跃时间抓取 + 自动跳过长期不活跃的 HR |
| 5 | Web 端无法看到 HR 上次活跃时间 | 每个岗位卡片展示 HR 活跃状态 + 公司去重状态 |

---

## 1. 公司去重（避免重复发同一家公司）

### `boss_state.py` 新增
- `_normalize_company_name()` — 去除 "有限公司/集团/股份" 等后缀做模糊匹配
- `has_company_been_applied(company_name)` — 查该公司是否已发过（applied/replied 视为已发，pending/skipped/failed 不算）
- `list_applied_companies()` — 列出所有已发过公司

### `boss_automation.py` 修改
- `apply_to_job()` 新增参数 `company_name` 和 `dedup_company`：在投递前先查 DB，跳过已发过公司
- `apply_batch()` 现在支持传入 `List[dict]`，每条带 `company` 字段

### 新增 DB 表
- `companies` 表缓存公司信息（行业/规模/员工数/成立时间/在招岗位/福利等），24h 内复用

---

## 2. 翻页扫描 + 批量投递

### `boss_automation.py` 新增方法
- `go_to_next_page()` — 智能点击 BOSS「下一页」按钮（兜底：直接修改 URL 的 `&page=N`）
- `scan_and_apply_all_pages(max_pages, dedup_company, filter_inactive_hr)` — 多页扫描投递

### `boss_app.py` 新增/修改
- `POST /api/jobs/scan-and-apply` — 新增 `max_pages`、`dedup_company`、`filter_inactive_hr` 三个参数

---

## 3. 岗位分析时展示公司信息

### `boss_automation.py` 新增方法
- `fetch_company_info(company_name, company_id, use_cache, max_age_hours)` — 抓取公司信息
  - 优先用 BOSS 公开 API：`/wapi/zpgeek/company/info.json` + `/wapi/zpgeek/company/positions.json`
  - API 失败时兜底访问 `/gongsi/{id}.html` 解析 DOM
  - 结果缓存到 `companies` 表

### `boss_firefox.py` 增强
- `_extract_job_cards()` 额外抓取 `companyId` / `brandName`（从 `/gongsi/<id>.html` 链接 + `data-ka` 属性）

### `boss_app.py` 修改
- `POST /api/jobs/analyze` — 新增 `with_company_info` / `company_id` 字段；返回中带 `company_info`（行业/融资/规模/员工数/成立/在招岗位等）

### 新增端点
- `POST /api/company/info` — 主动抓取公司信息
- `GET /api/company/cache/{name}` — 只查缓存
- `POST /api/company/check-applied` — 检查某公司是否已发过
- `GET /api/companies/applied` — 列出所有已发过公司

---

## 4. HR 活跃时间抓取 + 过滤

### `boss_firefox.py` 新增
- `parse_hr_active(text)` — 解析 BOSS 的 "刚刚活跃 / 3日内活跃 / 本周活跃 / 30日内" 等文案
- `_extract_job_cards()` 额外抓 `hrActive` 文本
- `fetch_detail()` 详情页 HR 信息里多抓一份活跃时间

### `boss_automation.py` 修改
- `apply_to_job()` 新增参数 `hr_active_days` / `hr_active_label` / `filter_inactive_hr`
- 超过 `max_hr_inactive_days`（默认 7 天）未活跃的 HR → 自动跳过
- 详情页投递后回写 `hr_active_label` / `hr_active_days` 到 DB

### `boss_state.py` 新增列
- `applications.hr_active_label` — 原文（如 "3日内活跃"）
- `applications.hr_active_days` — 数字天数（-1 = 未知）

### 新增设置（默认值）
- `max_hr_inactive_days` = `7`
- `filter_inactive_hr` = `true`
- `dedup_company_by_default` = `true`

---

## 5. Web 端 UI 更新 (`static/dashboard.html`)

### 搜索栏新增
- ☑️ 「公司去重」复选框
- ☑️ 「跳过HR N 天未活跃」复选框 + 数字输入（默认 7）
- 按钮「翻5页一键投递」

### 岗位卡片新增
- ⏱ HR 活跃时间标签（绿/橙/红三色对应不同新鲜度）
- ⚠️ 「已发过」标签（命中数据库去重）

### 分析 Modal 新增
- 「🏢 公司信息」区块，展示行业/规模/成立/员工数/在招岗位等

### 设置 Tab 新增
- 「智能过滤」分组：HR 活跃阈值、跳过不活跃 HR、默认公司去重

---

## 6. CLI 新增命令 (`lakejob_cli/`)

| 命令 | 说明 |
|------|------|
| `lakejob scan-apply --max-pages 5 --no-dedup --no-filter-inactive` | 多页扫描投递 |
| `lakejob scan-apply-all --max-pages 5` | 翻页一键投递 |
| `lakejob company-info <公司名> [--no-cache]` | 抓取公司信息 |
| `lakejob company-check <公司名>` | 检查公司是否已发过 |
| `lakejob companies-applied` | 列出所有已发过公司 |

---

## 7. 数据库 schema 迁移

启动时会自动 `ALTER TABLE` 添加新列（兼容旧库）：
- `applications.company_id` (TEXT)
- `applications.brand_name` (TEXT)
- `applications.hr_active_label` (TEXT)
- `applications.hr_active_days` (INTEGER)

新增表 `companies`（公司信息缓存）。

---

## 测试情况

| 测试 | 结果 |
|------|------|
| 公司去重（精确/后缀变体/skipped 不算） | ✅ |
| 公司信息缓存 + 24h 复用 | ✅ |
| HR 活跃时间解析（10+ 场景） | ✅ |
| HR 活跃度过滤（边界 7/8 天） | ✅ |
| 设置读写 | ✅ |
| 模块导入 | ✅ |
| CLI 帮助 | ✅ |
| FastAPI Pydantic 模型 | ✅ |
