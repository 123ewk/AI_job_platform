"""agent/tools.py — Agent 工具（SDD Step 3.1 只读 / 3.2 写配置 / 3.3 浏览器+会话概览，§4.2 工具清单）。

实现真工具（相对 2.4 的 echo 假工具）：
- `query_jobs`：只读 DB 查岗位库，`status` 精确过滤 + `ungreeted=true` 专用过滤
  （打招呼流程的第一步必须先查库存再搜新，§4.2 硬规则）；可选 city/keyword/分页。
- `get_progress`：今日已投 / 每日额度 / 剩余额度（Agent 自律"额度用完就停"的前提）。
- `update_setting`：写 settings 配置（write=True 走审批门）。白名单
  `state.SETTINGS_WHITELIST`（== 手动设置 API 字段集）；敏感键 `state.SENSITIVE_SETTING_KEYS`
  全模式硬拒（唯一可写路径是人工 /api/settings）；值经 `state.mask_sensitive` 脱敏。
- `search_jobs`：**读浏览器**搜新岗位（§4.2 分类 write=False）——调既有 `BossScraper.search`
  走 `_run_pw` 单线程池，`max_pages≤3` 翻页；入库 `status=discovered`、按 URL 去重、
  被过滤的恢复 pending；浏览器互斥走 `FlowLock`（§4.6），**锁被占时排队等待而非报错**。
- `get_conversations_summary`：本地镜像库会话概览（用户问"有没有 HR 回我"，不碰浏览器）。

设计要点：
- **status 机映射**：`agent.state.JobStatus` 是 Agent 岗位状态机单一真源，值直接写/读
  现有 `applications.status` 列（与 dashboard 去重口径共享），工具不另立平行列。
- **L3 参数校验**：工具用 Pydantic 模型定义入参（§4.2），schema 经
  `interview.llm_client.build_tool_schema` 转 OpenAI tools 声明；执行时 `Model(**kwargs)`
  先校验再查库。**校验失败返回 `{"error": ...}` 结果而非抛异常**——错误作为工具输出
  回灌 plan，LLM 据此自纠（禁止 Agent 编默认值，§4.4）。
- **敏感键硬拒**：update_setting 对 ai_api_key/wechat_id 无条件拒绝（实现时定的最严解释），
  日志只记掩码值，返回结果也不回显原始值。
- **FlowLock 互斥（§4.6）**：search_jobs 执行期间持有浏览器互斥锁，`chat_monitor_loop`
  每轮非阻塞查询被占则跳过；锁被占时工具阻塞 `acquire(owner, blocking=True)` 排队。
- **引擎注入**：工具以 factory 闭包绑定注入的 SQLA 引擎（测试用内存库、运行时真实库），
  `_execute_tool` 调 `func(**args)` 时 LLM 参数与 engine 无键冲突。
- **浏览器桥可注入**：search_jobs 的 `get_automation` / `pw_runner` / `lock` 均可注入
  （测试用假浏览器 + 直调桥，不启动 Playwright）；缺省懒加载 `boss_app`（避免循环导入，
  运行时 boss_app 已完整导入）。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session as SASession

from agent import state
from agent.flow_lock import FlowLock, default_flow_lock
from agent.graph import ToolRegistry
from db import models
from interview.llm_client import build_tool_schema

logger = logging.getLogger("agent.tools")

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


class UpdateSettingParams(BaseModel):
    """update_setting 入参。key 必须在 `agent.state.SETTINGS_WHITELIST`（=手动设置 API 字段集）。"""

    key: str = Field(
        ...,
        description="settings 配置键（白名单内；敏感键 ai_api_key/wechat_id 一律拒绝，需人工在 /api/settings 修改）",
    )
    value: str = Field("", description="新值（字符串；清空传空串）")


# BOSS 求职类型 4 位 code（boss_firefox.search 的 jobType URL 参数；1902=实习 实测可过滤 ~75%+ 实习岗）
_JOB_TYPE_ALIASES: dict[str, str] = {
    "全职": "1901", "full": "1901", "full-time": "1901", "fulltime": "1901", "regular": "1901",
    "实习": "1902", "实习生": "1902", "practice": "1902", "intern": "1902", "internship": "1902",
    "兼职": "1903", "part": "1903", "part-time": "1903", "parttime": "1903",
    "1": "1901", "2": "1903", "3": "1902",
    "1901": "1901", "1902": "1902", "1903": "1903",
}


class SearchJobsParams(BaseModel):
    """search_jobs 入参（§4.2：读浏览器，调既有 BossScraper.search，入库 status=discovered）。"""

    keyword: str = Field(..., description="搜索关键词")
    city: str | None = Field(None, description="城市名（缺省取 settings default_city，再缺省'全国'）")
    max_pages: int = Field(1, ge=1, le=3, description="搜索页数上限（1-3）")
    job_type: str | None = Field(
        None,
        description=(
            "求职类型筛选，可选。用户提到全职/实习/兼职时必须传："
            "全职=1901、实习=1902、兼职=1903（也接受中文或 full/internship/part-time）；"
            "用户没提类型就不要传"
        ),
    )

    @field_validator("job_type")
    @classmethod
    def _normalize_job_type(cls, v: str | None) -> str | None:
        """别名统一成 BOSS 4 位 code；白名单外直接 ValidationError（L3 错误回灌 LLM 自纠）。"""
        if v is None or not str(v).strip():
            return None
        code = _JOB_TYPE_ALIASES.get(str(v).strip().lower())
        if code is None:
            raise ValueError("job_type 只支持：全职(1901) / 实习(1902) / 兼职(1903)")
        return code


class ConversationsSummaryParams(BaseModel):
    """get_conversations_summary 入参（§4.2：本地镜像库会话概览，不碰浏览器）。"""

    limit: int = Field(10, ge=1, le=50, description="返回会话条数上限（1-50）")
    only_unread: bool | None = Field(None, description="只统计有未读的会话（可选）")
    hr_name: str | None = Field(None, description="按 HR 姓名精确过滤（可选）")


class SendGreetingsParams(BaseModel):
    """send_greetings 入参（§4.2：打招呼后台长任务）。

    一次最多给 `max_count` 个岗位发招呼语，取 `min(max_count, 今日剩余额度)`。
    `keyword`（V1.3.0）：用户指定想投的关键词时必传——只投岗位名/公司命中的库存，
    避免把鱼龙混杂的整批库存全部投出去（手动端同步有关键词过滤框 + 勾选投递）。
    """

    max_count: int = Field(10, ge=1, le=50, description="本次最多打招呼岗位数（1-50，实际取 min(max_count, 今日余量)）")
    keyword: str | None = Field(
        None,
        description=(
            "投递关键词筛选（可选）。用户说了想投哪类岗位（如'只投大模型''Agent 相关的'）时必须传；"
            "只投岗位名或公司名包含该关键词的库存岗位。用户没提关键词就不要传，但按规则应先反问用户"
        ),
    )


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
            # Step 5.3：上报全局 DRY_RUN 演练标志（Agent 据此汇报"当前是演练模式，不会实际发送"）
            "dry_run": _get_dry_run(engine),
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


def update_setting_factory(engine) -> Callable[..., dict]:
    """构造 `update_setting(**kwargs) -> dict`：写 settings（write=True，走审批门）。

    `key` 必填、`value` 可缺省——Pydantic `UpdateSettingParams(**kwargs)` 先校验（L3），
    缺 key 返回 `{"error":...}` 而非 TypeError。

    安全边界（spec §3.2/§5.3）：
    1. **白名单**：key 不在 `SETTINGS_WHITELIST` → 拒绝（回 `allowed` 清单供 LLM 自纠）。
    2. **敏感键**：ai_api_key/wechat_id **全模式硬拒**（autonomous 也不放过，实现时定的最严
       解释；Agent 无路径改敏感键，唯一可写路径是人工 /api/settings）。日志只记掩码。
    3. **系统级安全开关**（Step 5.3）：dry_run 等 `SAFETY_SETTING_KEYS` **全模式硬拒**——Agent
       不得关闭/绕过 DRY_RUN 演练保护（§4.3"系统级安全规则不可被 LLM 覆盖"），唯一可写路径
       是人工 /api/settings。
    4. **不落原始值**：返回结果回显的值经 `mask_sensitive`，transcript 由 graph 层统一脱敏。
    """

    def update_setting(**kwargs: Any) -> dict:
        try:
            p = UpdateSettingParams(**kwargs)
        except ValidationError as e:
            return {"error": "参数校验失败", "message": str(e)}
        key, value = p.key, p.value
        if key not in state.SETTINGS_WHITELIST:
            return {
                "error": "配置键不在白名单",
                "message": f"key={key} 不在 Agent 可写设置白名单内（=手动设置 API 的字段集）",
                "allowed": sorted(state.SETTINGS_WHITELIST),
            }
        if key in state.SENSITIVE_SETTING_KEYS:
            # 敏感键：日志只记键名与掩码占位，绝不回显原始值
            logger.warning("update_setting 敏感键被拒：key=%s，value 已掩码不回显", key)
            return {
                "error": "敏感配置键拒绝",
                "message": (
                    f"key={key} 是敏感配置键（{sorted(state.SENSITIVE_SETTING_KEYS)}），"
                    "Agent 无权修改；请人工在 /api/settings 修改"
                ),
                "masked": True,
            }
        if key in state.SAFETY_SETTING_KEYS:
            # 系统级安全开关（Step 5.3 DRY_RUN 演练保护）：Agent 不得关闭/绕过演练保护
            logger.warning("update_setting 安全开关被拒：key=%s（DRY_RUN 演练保护，仅人工可改）", key)
            return {
                "error": "系统级安全开关拒绝",
                "message": (
                    f"key={key} 是系统级安全开关（DRY_RUN 演练保护），Agent 不得关闭或绕过演练保护；"
                    "请人工在 /api/settings 修改"
                ),
                "safety": True,
            }
        with SASession(engine) as s, s.begin():
            if s.get(models.Setting, key) is not None:
                s.execute(
                    update(models.Setting)
                    .where(models.Setting.key == key)
                    .values(value=value, updated_at=func.current_timestamp())
                )
            else:
                s.execute(models.Setting.__table__.insert().values(key=key, value=value))
        logger.info("update_setting 已写入：key=%s", key)
        return {"error": None, "key": key, "value": state.mask_sensitive(value), "updated": True}

    return update_setting


# ══════════════════════════════════════════════════════════
#  浏览器互斥 + 搜索 / 会话概览（Step 3.3，§4.2/§4.6）
# ══════════════════════════════════════════════════════════


def _get_setting_value(engine, key: str, default: str) -> str:
    with SASession(engine) as s:
        row = s.get(models.Setting, key)
    return row.value if row is not None and row.value else default


def _get_dry_run(engine) -> bool:
    """读全局 dry_run 设置（Step 5.3 DRY_RUN 演练开关）。真值集：1/true/yes/on（大小写不敏感）。"""
    return _get_setting_value(engine, "dry_run", "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_url(url: str) -> str:
    """规范化 BOSS 岗位 URL（懒加载 boss_app._normalize_job_url，避免循环导入）。"""
    from boss_app import _normalize_job_url  # noqa: PLC0415 运行时 boss_app 已完整导入

    return _normalize_job_url(url)


def _int_or_neg(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _default_pw_runner(fn, *args, **kwargs) -> Any:
    """工具线程里跑 `_run_pw` 的默认桥：懒加载 boss_app，`asyncio.run` 起临时循环。

    search_jobs 在 `asyncio.to_thread` 工作线程执行（无运行中事件循环），故在工具线程
    里 `asyncio.run(_run_pw(...))` 起一个临时循环，把同步浏览器操作提交到 boss_app 的
    pw 单线程池串行执行——与既有端点完全同一条执行路径，只是等待方换了个临时循环。

    `_run_pw` 必须经 live_boss_app() 取：它闭包持有**各自副本**的 _playwright_executor，
    从影子副本拿会让 start 与工具操作落在不同 OS 线程——Playwright sync 对象绑线程，
    直接 greenlet "Cannot switch to a different thread"（V1.2.26 用户实测）。
    """
    import asyncio

    return asyncio.run(live_boss_app()._run_pw(fn, *args, **kwargs))


def live_boss_app() -> Any:
    """解析"活着的" boss_app 模块（V1.2.26 hotfix）。

    `python boss_app.py` 启动时服务器模块名是 __main__；本包再 `import boss_app` 会把
    文件再执行一遍生成影子副本——端点更新的是 __main__ 上的可变全局（automation /
    monitor_paused / app.state），影子副本永远停在初始值，浏览器工具因此恒报
    "浏览器未启动"（用户实测：浏览器明明已启动）。判据：__main__.__file__ 是
    boss_app.py 就优先 __main__，否则（uvicorn boss_app:app 启动）用 boss_app 本尊。
    """
    import sys

    main_mod = sys.modules.get("__main__")
    if main_mod is not None and str(getattr(main_mod, "__file__", "") or "").endswith("boss_app.py"):
        return main_mod
    import boss_app as app_mod  # noqa: PLC0415

    return app_mod


def _default_get_automation() -> Any:
    return getattr(live_boss_app(), "automation", None)


def _ready_probe(loader: Callable[[], Any]) -> Callable[[], bool]:
    """由（注入的）loader 构造就绪探针：automation 与 page 都在才算就绪。

    只查对象存在性、不跨线程调 Playwright 方法（预检必须零成本）；窗口被关但
    对象还在的场景交给工具运行时异常 + friendly_error 兜底。
    """

    def ready() -> bool:
        a = loader()
        return a is not None and getattr(a, "page", None) is not None

    return ready


def _search_page_n(automation, keyword: str, city_code: str, page: int, job_type: str | None = None) -> list[dict]:
    """翻到搜索第 N 页并提取岗位卡片（整段在 pw 线程里执行）。

    第 1 页走既有 `automation.search`（§4.2"调既有 BossScraper.search"）；后续页
    BOSS 分页 URL（`page=N`）+ 复用既有 `_wait_for_jobs_loaded`/`_scroll_all`/
    `_extract_job_cards`（boss_firefox.py 一行不动，仅复用其方法）。
    job_type（V1.2.27）必须带上——翻页 URL 丢了 jobType，第 2 页起类型筛选会静默失效。
    """
    from urllib.parse import urlencode

    params: dict[str, Any] = {"query": keyword, "city": city_code, "page": page}
    if job_type:
        params["jobType"] = job_type
    url = "https://www.zhipin.com/web/geek/job?" + urlencode(params)
    automation.page.goto(url, wait_until="domcontentloaded", timeout=45000)
    automation._wait_for_jobs_loaded(min_count=5, max_wait_s=10)
    automation._scroll_all()
    return automation._extract_job_cards()


def _search_pages(
    runner: Callable[..., Any], automation, keyword: str, city_code: str, max_pages: int,
    job_type: str | None = None,
) -> list[dict]:
    """逐页搜岗位：第 1 页走 automation.search（job_type 透传），2..max_pages 走翻页 URL。"""
    jobs: list[dict] = []
    for page in range(1, max_pages + 1):
        if page == 1:
            page_jobs = runner(automation.search, keyword, city_code, job_type=job_type or "") or []
        else:
            page_jobs = runner(_search_page_n, automation, keyword, city_code, page, job_type) or []
        if page_jobs:
            jobs.extend(page_jobs)
    return jobs


def _persist_discovered(engine, jobs: list[dict]) -> tuple[int, int, int]:
    """去重 + 入库：新岗位 status='discovered'，已存在按 URL 去重，被过滤的恢复 pending。

    §4.2 返回"新增 N 条 / 去重 M 条"。去重口径与存量 `/api/jobs/search` 一致
    （job_url 唯一）；不套用 boss_app 的关键词黑名单/HR 活跃度过滤——那是打招呼流程
    消费端的过滤，Phase 4.2 send_greetings 沿用既有逻辑。
    """
    added = 0
    deduped = 0
    restored = 0
    with SASession(engine) as s:
        for j in jobs:
            url = _normalize_url(j.get("url") or "")
            if not url:
                continue
            existing = s.execute(
                select(models.Application).where(models.Application.job_url == url)
            ).scalar_one_or_none()
            if existing is not None:
                deduped += 1
                if existing.status == state.JobStatus.FILTERED:
                    # 之前被关键词过滤、现重新搜到 → 恢复为可打招呼库存（与既有搜索口径一致）
                    existing.status = state.JobStatus.PENDING
                    restored += 1
                continue
            s.add(
                models.Application(
                    job_title=j.get("title") or "",
                    company=j.get("company"),
                    salary=j.get("salary"),
                    job_url=url,
                    city=j.get("city"),
                    experience=j.get("experience"),
                    education=j.get("education"),
                    hr_name=j.get("hr_name"),
                    hr_title=j.get("hr_title"),
                    description=j.get("description"),
                    company_id=j.get("company_id"),
                    brand_name=j.get("brand_name"),
                    hr_active_label=j.get("hr_active_label"),
                    hr_active_days=_int_or_neg(j.get("hr_active_days")),
                    status=state.JobStatus.DISCOVERED,
                )
            )
            added += 1
        s.commit()
    return added, deduped, restored


def search_jobs_factory(
    engine,
    *,
    lock: FlowLock | None = None,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
) -> Callable[..., dict]:
    """构造 `search_jobs(**kwargs) -> dict`：读浏览器搜岗位 + 入库 status=discovered。

    §4.2：调既有 `BossScraper.search`（走 `_run_pw` 单线程池），新增入库 discovered、
    已存在按 URL 去重、此前被过滤的恢复 pending。浏览器互斥用 FlowLock（§4.6）——
    **锁被占时阻塞排队等待而非报错**（Step 3.3 验收测试焦点）。

    - `lock`：浏览器互斥锁（缺省模块单例 `default_flow_lock`；测试注入独立锁）
    - `get_automation()`：取浏览器对象（缺省懒加载 `boss_app.automation`；None=未启动）
    - `pw_runner(fn, *args) -> result`：把同步浏览器操作桥到 pw 单线程池
      （缺省 `_default_pw_runner`；测试注入同步直调）
    """
    flow = lock if lock is not None else default_flow_lock
    loader = get_automation or _default_get_automation
    runner = pw_runner or _default_pw_runner

    def search_jobs(**kwargs: Any) -> dict:
        try:
            p = SearchJobsParams(**kwargs)
        except ValidationError as e:
            return {"error": "参数校验失败", "message": str(e)}

        # §4.6：浏览器互斥——阻塞排队等待（不报错、不并发抢浏览器），拿到锁才动浏览器
        if not flow.acquire(f"agent:search_jobs:{p.keyword}", blocking=True):
            return {"error": "浏览器忙", "message": "浏览器互斥锁被占用（排队超时）"}
        try:
            automation = loader()
            if automation is None:
                return {
                    "error": "浏览器未启动",
                    "message": "控制台的自动化浏览器没有在运行（注意：扫码登录≠浏览器已启动，两者独立）。"
                    "请用户在控制台首页点「启动浏览器」后，再原样重试本工具。",
                }

            from boss_app import CITY_MAP  # noqa: PLC0415

            city = p.city or _get_setting_value(engine, "default_city", "全国")
            city_code = CITY_MAP.get(city, "100010000")
            jobs = _search_pages(runner, automation, p.keyword, city_code, p.max_pages, p.job_type)
            added, deduped, restored = _persist_discovered(engine, jobs)
            return {
                "error": None,
                "keyword": p.keyword,
                "city": city,
                "job_type": p.job_type,
                "pages": p.max_pages,
                "found": len(jobs),
                "added": added,
                "deduped": deduped,
                "restored_from_filtered": restored,
            }
        finally:
            flow.release()

    return search_jobs


def get_conversations_summary_factory(engine) -> Callable[..., dict]:
    """构造 `get_conversations_summary(**kwargs) -> dict`：本地镜像库会话概览（§4.2）。

    用户问"有没有 HR 回我"：不碰浏览器，从 conversations/messages 镜像库回答。
    `last_message_text` 出工具前经 `state.mask_sensitive` 脱敏（手机号/token 掩码）；
    不输出 hr_wechat 字段（Agent 不需要，杜绝泄露面）。
    """

    def get_conversations_summary(**kwargs: Any) -> dict:
        try:
            p = ConversationsSummaryParams(**kwargs)
        except ValidationError as e:
            return {"error": "参数校验失败", "message": str(e)}

        filters = []
        if p.only_unread:
            filters.append(models.Conversation.unread_count > 0)
        if p.hr_name:
            filters.append(models.Conversation.hr_name == p.hr_name)

        with SASession(engine) as s:
            total = s.execute(
                select(func.count()).select_from(models.Conversation).where(*filters)
            ).scalar_one()
            unread_total = s.execute(
                select(func.count()).select_from(models.Conversation).where(
                    models.Conversation.unread_count > 0, *filters
                )
            ).scalar_one()
            rows = (
                s.execute(
                    select(models.Conversation)
                    .where(*filters)
                    .order_by(models.Conversation.updated_at.desc())
                    .limit(p.limit)
                )
                .scalars()
                .all()
            )

        conversations = [
            {
                "id": c.id,
                "hr_name": c.hr_name,
                "hr_company": c.hr_company,
                "job_title": c.job_title,
                "last_message_from": c.last_message_from,
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
                "last_message_text": state.mask_sensitive(c.last_message_text) if c.last_message_text else None,
                "unread_count": c.unread_count,
                "online_status": c.online_status,
            }
            for c in rows
        ]
        return {
            "error": None,
            "total": total,
            "unread_total": unread_total,
            "count": len(conversations),
            "conversations": conversations,
        }

    return get_conversations_summary


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


def build_write_tools(engine, registry: ToolRegistry | None = None) -> ToolRegistry:
    """注册写配置工具到 registry（缺省新建）。write=True → audit 模式过审批门。"""
    reg = registry or ToolRegistry()
    reg.register(
        "update_setting",
        func=update_setting_factory(engine),
        description=(
            "改 settings 配置（写，audit 模式下挂起等确认）。key 必须落在白名单（=手动设置 API 字段集）；"
            "敏感键 ai_api_key/wechat_id 一律拒绝，请人工在 /api/settings 修改"
        ),
        schema=build_tool_schema("update_setting", "改配置（写，白名单 + 敏感键拒绝）", UpdateSettingParams),
        write=True,
    )
    return reg


def build_browser_tools(
    engine,
    registry: ToolRegistry | None = None,
    *,
    lock: FlowLock | None = None,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    set_automation: Callable[[Any], None] | None = None,
    start_browser: Callable[[], Any] | None = None,
) -> ToolRegistry:
    """注册 search_jobs + get_conversations_summary + open/close_browser 到 registry（缺省新建）。

    - `search_jobs` 是"读浏览器"工具（§4.2 分类）→ write=False，audit 直放；持有
      FlowLock 互斥（§4.6），锁被占时排队而非并发；`requires_browser=True`（execute 前
      预检浏览器，未启动秒返引导报错）。
    - `get_conversations_summary` 纯本地镜像库只读，不碰浏览器、不拿锁。
    - `open_browser` / `close_browser` 浏览器生命周期工具（自愈用），write=False。
    `lock/get_automation/pw_runner/set_automation/start_browser` 透传给各 factory（测试注入）。
    """
    reg = registry or ToolRegistry()
    reg.register(
        "search_jobs",
        func=search_jobs_factory(engine, lock=lock, get_automation=get_automation, pw_runner=pw_runner),
        description=(
            "搜新岗位（读浏览器，入库 status=discovered）。浏览器被其他流程占用时排队等待（FlowLock 互斥），"
            "不并发抢占"
        ),
        schema=build_tool_schema(
            "search_jobs",
            "用浏览器在 BOSS 直聘搜新岗位并入库。用户指定了求职类型（全职/实习/兼职）时"
            "必须传 job_type 参数（1901/1902/1903），否则搜回来的岗位类型不可控",
            SearchJobsParams,
        ),
        write=False,
        requires_browser=True,
        browser_ready=_ready_probe(get_automation or _default_get_automation),
    )
    reg.register(
        "get_conversations_summary",
        func=get_conversations_summary_factory(engine),
        description="查本地镜像库会话概览（只读，不碰浏览器）。回答'有没有 HR 回我'",
        schema=build_tool_schema("get_conversations_summary", "查会话概览（只读）", ConversationsSummaryParams),
        write=False,
    )
    reg.register(
        "open_browser",
        func=open_browser_factory(
            get_automation=get_automation,
            pw_runner=pw_runner,
            start_browser=start_browser,
            set_automation=set_automation,
        ),
        description=(
            "启动自动化浏览器（幂等：已在运行就直接返回，窗口被手动关掉时会自动重开）。"
            "收到「浏览器未启动」类 error 后先用本工具恢复，再原样重试原工具"
        ),
        schema=build_tool_schema("open_browser", "启动自动化浏览器（幂等，无参数）", BrowserLifecycleParams),
        write=False,
    )
    reg.register(
        "close_browser",
        func=close_browser_factory(
            get_automation=get_automation, pw_runner=pw_runner, set_automation=set_automation, lock=lock
        ),
        description=(
            "关闭自动化浏览器释放资源（幂等：未启动时直接返回）。有任务正在用浏览器时会被拒绝；"
            "关闭后聊天监控会自动停止，需要时再 open_browser 或在控制台重启"
        ),
        schema=build_tool_schema("close_browser", "关闭自动化浏览器（幂等，无参数）", BrowserLifecycleParams),
        write=False,
    )
    return reg


# ══════════════════════════════════════════════════════════
#  浏览器生命周期工具（open_browser / close_browser，自愈用）
# ══════════════════════════════════════════════════════════


class BrowserLifecycleParams(BaseModel):
    """open_browser / close_browser 不需要参数（占位 schema 用）。"""


def _new_automation() -> Any:
    """真正的启动动作（必须在 pw 单线程里执行：Playwright sync 对象绑创建线程）。"""
    from boss_automation import BossAutomation  # noqa: PLC0415

    a = BossAutomation(headless=False)
    a.start()
    return a


def _default_set_automation(a: Any) -> None:
    """启动/关闭后回写 boss_app 全局 automation（经 live 模块，防影子副本 V1.2.26）。"""
    setattr(live_boss_app(), "automation", a)


def open_browser_factory(
    *,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    start_browser: Callable[[], Any] | None = None,
    set_automation: Callable[[Any], None] | None = None,
) -> Callable[..., dict]:
    """构造 `open_browser(**kwargs) -> dict`：启动自动化浏览器（幂等 + 自愈）。

    - 已在运行（对象存在且 page 在）→ 心跳探针确认后原样返回，不重复开；
    - 探针失败（窗口被手动关掉等）→ 先收尾旧对象再重开——这是"浏览器被关但对象
      还在"场景的自愈路径，预检（graph 侧）对它是放行的；
    - 未启动 → pw 单线程里 BossAutomation(headless=False).start()（启动必须与后续
      浏览器操作同线程，V1.2.26 教训），成功后回写 boss_app 全局。

    测试注入：`get_automation/pw_runner/start_browser/set_automation` 全部可替换。
    """
    loader = get_automation or _default_get_automation
    runner = pw_runner or _default_pw_runner
    starter = start_browser or _new_automation
    setter = set_automation or _default_set_automation

    def open_browser(**kwargs: Any) -> dict:
        cur = loader()
        if cur is not None and getattr(cur, "page", None) is not None:
            try:
                runner(cur.heartbeat)
                return {"error": None, "status": "already_running", "message": "浏览器已经在运行，无需重复开启"}
            except Exception as e:
                from boss_firefox import BrowserThreadMismatchError  # noqa: PLC0415

                if isinstance(e, BrowserThreadMismatchError):
                    # 旧对象绑在旧线程上：close 同样跨线程做不掉，旧 Firefox 进程还活着
                    # 并占着 profile 锁——此刻重开必撞 parent.lock 挂死。拒绝自动重建，
                    # 把恢复权交还给人（重启服务进程）。
                    return {"error": "浏览器线程失效", "message": str(e)}
                try:
                    runner(cur.close)
                except Exception:
                    pass
        automation = runner(starter)
        setter(automation)
        return {
            "error": None,
            "status": "started",
            "message": "浏览器已启动（登录态自动恢复）。若此前聊天监控已停止，请在控制台重启一次监控",
        }

    return open_browser


def close_browser_factory(
    *,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    set_automation: Callable[[Any], None] | None = None,
    lock: FlowLock | None = None,
) -> Callable[..., dict]:
    """构造 `close_browser(**kwargs) -> dict`：关闭自动化浏览器（幂等 + 护栏）。

    护栏：FlowLock 被占（搜索/发招呼任务进行中）→ 拒绝关闭，等任务结束再关。
    """
    loader = get_automation or _default_get_automation
    runner = pw_runner or _default_pw_runner
    setter = set_automation or _default_set_automation
    flow = lock if lock is not None else default_flow_lock

    def close_browser(**kwargs: Any) -> dict:
        cur = loader()
        if cur is None or getattr(cur, "page", None) is None:
            return {"error": None, "status": "not_running", "message": "浏览器本来就未启动，无需关闭"}
        if flow.locked():
            return {"error": "浏览器忙", "message": "有任务正在使用浏览器（搜索/发招呼进行中），等任务结束后再关闭"}
        runner(cur.close)
        setter(None)
        return {
            "error": None,
            "status": "closed",
            "message": "浏览器已关闭。聊天监控会在心跳失败后自动停止；需要时再 open_browser 或在控制台重启",
        }

    return close_browser


# ══════════════════════════════════════════════════════════
#  send_greetings 后台打招呼任务（Step 4.2，§4.2 写路径最高风险区）
# ══════════════════════════════════════════════════════════


def _application_job_row(r: Any) -> dict:
    """Application ORM 行 → 打招呼单位用的 job dict（字段名对齐 apply_batch 新 API）。"""
    return {
        "id": r.id,
        "job_url": r.job_url,
        "job_title": r.job_title,
        "title": r.job_title,
        "company": r.company,
        "company_id": r.company_id,
        "hr_active_days": r.hr_active_days,
        "hr_active_label": r.hr_active_label,
        "description": r.description,
        "city": r.city,
    }


def _resolve_greeting(engine, jobs: list[dict]) -> str:
    """resolve 一次招呼语供整批复用（与 apply_batch 头部同款逻辑：模板优先，smart 走 LLM）。

    只 resolve 一次，避免 per-unit 反复 LLM 生成（apply_batch 自身"第一条生成、后续复用"）
    的思路；传入单位后 apply_batch 不再生成。沿用既有 setting 键 + `generate_greeting`（不重写）。
    """
    template = _get_setting_value(engine, "greeting_template", "")
    if template:
        return template
    first = jobs[0]
    title = (first.get("job_title") or first.get("title") or "相关岗位")
    company = (first.get("company") or "贵公司")
    jd_text = first.get("description") or ""
    style = _get_setting_value(engine, "ai_reply_style", "professional")
    if _get_setting_value(engine, "greeting_mode", "template") == "smart":
        from boss_replier import generate_greeting  # noqa: PLC0415  既有 LLM 生成，不重写

        return generate_greeting(title, company, style=style, jd_text=jd_text, smart=True)
    return "您好，我对贵公司的{job_title}岗位很感兴趣，请问可以详细了解一下吗？"


_LEFTOVER_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _render_greeting(greeting: str, job: dict) -> str:
    """按岗位实例化招呼语占位符（{job_title}/{company}），语义对齐 boss_replier.generate_greeting。

    V1.2.28 修复：settings 里的 greeting_template 是带占位符的**模板**，手动路径经
    generate_greeting 的 .replace() 替换后才发送；Agent 批量路径此前把原始模板整批直发
    （用户实测：BOSS 上HR 收到字面 "{job_title}"）。现在逐岗位替换；替换后仍残留任何
    变量形占位符（如模板里写了不支持的 {hr_name}）则兜底成通用句，绝不把花括号原文发给 HR。
    """
    title = ((job.get("job_title") or job.get("title") or "").strip() or "相关岗位")
    company = ((job.get("company") or "").strip() or "贵公司")
    text = (greeting or "").replace("{job_title}", title).replace("{company}", company)
    if _LEFTOVER_PLACEHOLDER.search(text):
        text = f"您好，我对贵公司的{title}岗位很感兴趣，请问可以详细了解一下吗？"
    return text


def _mark_greeted(engine, job_url: str, greeting: str) -> None:
    """写库：apply_batch 成功后置 Agent 侧 'greeted' + 招呼语 + 时间戳（§4.2 '先写库再发下一个'）。

    apply_batch 内部会把 record 置 'applied'；Agent 侧再补 'greeted' 以便 query_jobs(ungreeted)
    排除已打招呼集，且与 dashboard 共享同一 applications.status 列。
    """
    with SASession(engine) as s:
        row = s.execute(
            select(models.Application).where(models.Application.job_url == job_url)
        ).scalar_one_or_none()
        if row is not None:
            row.status = state.JobStatus.GREETED
            if greeting:
                row.greeting_text = greeting
            row.greeting_sent_at = datetime.now()
            s.commit()


def build_greeting_unit(
    engine,
    jobs: list[dict],
    greeting: str,
    *,
    lock: FlowLock | None = None,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    dry_run: bool = False,
) -> Callable[[int], dict]:
    """构造打招呼单位函数 `unit(i)`（i 从 1 开始，与 executor 单位下标一致），供后台任务驱动。

    每个单位 = 一个岗位的一次打招呼，**逐岗位"先写库再发下一个"**：
    1. 持有 FlowLock（§4.6，浏览器互斥，chat_monitor_loop 被占时跳本轮让路）→ 包既有
       `apply_batch` 跑单岗位（日限 / 公司去重 / HR 活跃过滤**全部沿用其内部逻辑，不重写**）；
    2. 成功后 `_mark_greeted` 写库（置 greeted + 招呼语 + 时间戳）；
    再由 executor 在单位之间检查停止标志并在进入下一单位前落 progress_done——结束当前 DB 语义
    才可见"发下一个"。`greeting` 若是带 {job_title}/{company} 占位符的模板，每个单位发送
    前按本岗位实例化（`_render_greeting`，V1.2.28）。

    `dry_run=True`（Step 5.3 DRY_RUN 演练）：本单位**只记"将要发送"，不发浏览器、不改状态**
    （不拿锁、不调 apply_batch、不 _mark_greeted）——演练不消耗真实库存/今日额度，返回
    `{"dry_run": True, "would_send": {...}}` 载荷；job 保持 ungreeted，可安全重来。
    """
    flow = lock if lock is not None else default_flow_lock
    loader = get_automation or _default_get_automation
    runner = pw_runner or _default_pw_runner

    def _unit(i: int) -> dict:
        job = jobs[i - 1]
        url = job["job_url"]
        # V1.2.28：整批 resolve 的 greeting 可能是带占位符的模板——发送前按本岗位实例化
        greeting_text = _render_greeting(greeting, job)
        if dry_run:
            # Step 5.3：DRY_RUN 演练——只记"将要发送"，绝不碰浏览器/绝不改状态（不 _mark_greeted）。
            title = job.get("title") or job.get("job_title") or ""
            company = job.get("company") or ""
            logger.warning("DRY_RUN 演练：将要发送「%s」@「%s」（%s），未实际发送", title, company, url)
            return {
                "dry_run": True,
                "would_send": {
                    "job_url": url, "title": title, "company": company, "greeting": greeting_text,
                },
                "job_url": url,
                "success": True,
                "message": "DRY_RUN：未实际发送（演练）",
            }
        owner = f"agent:send_greetings:{(url or 'job')[-24:]}"
        # §4.6：单位内拿锁（阻塞排队），HR 监控轮询非阻塞让路
        if not flow.acquire(owner, blocking=True):
            return {"error": "浏览器忙", "message": "浏览器互斥锁被占用（排队超时）", "job_url": url}
        try:
            automation = loader()
            if automation is None:
                return {
                    "error": "浏览器未启动",
                    "message": "控制台的自动化浏览器没有在运行（扫码登录≠浏览器已启动），"
                    "请用户在控制台首页点「启动浏览器」后重试。",
                    "job_url": url,
                }
            job_dict = {
                "url": url,
                "title": job.get("title") or job.get("job_title") or "",
                "company": job.get("company") or "",
                "company_id": job.get("company_id"),
                "hr_active_days": job.get("hr_active_days"),
                "hr_active_label": job.get("hr_active_label", ""),
                "description": job.get("description") or "",
            }
            res = runner(automation.apply_batch, jobs=[job_dict], greeting_template=greeting_text) or []
            r = res[0] if res else {"success": False, "message": "apply_batch 无返回"}
            if r.get("success"):
                _mark_greeted(engine, url, greeting_text)
            return {**r, "job_url": url}
        finally:
            flow.release()

    return _unit


def _default_executor() -> Any:
    """执行器缺省解析：桥到 boss_app 的 app.state.agent_executor（api._get_executor），
    进度/终态经其 broadcast 接到 AgentHub → /ws/agent 广播。懒加载避免循环导入；
    app 必须经 live_boss_app() 取（影子副本的 app.state 没有 executor，见 live_boss_app 文档）。"""
    import types

    from agent.api import _get_executor  # noqa: PLC0415

    return _get_executor(types.SimpleNamespace(app=getattr(live_boss_app(), "app", None)))


def _default_paused() -> bool:
    """用户暂停标志缺省解析（懒加载 boss_app.monitor_paused；测试注入 paused=False 则不碰）。"""
    return bool(getattr(live_boss_app(), "monitor_paused", False))


def send_greetings_factory(
    engine,
    *,
    executor=None,
    lock: FlowLock | None = None,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    paused: Callable[[], bool] | None = None,
) -> Callable[..., dict]:
    """构造 `send_greetings(**kwargs) -> dict`：提交一个后台打招呼长任务（write=True 走审批门）。

    工具本体**不碰浏览器、不阻塞对话**——只做三件事就返回 task_id：
    1. L3 校验 + 尊重用户暂停（§4.6） + 今日额度（沿用 get_progress 的
       `min(daily_apply_limit, MAX_APPLY_PER_DAY)` 口径）；
    2. 查 ungreeted（status∈GREETABLE）库存，取 `min(max_count, 剩余额度)`；
    3. `executor.submit(kind="send_greetings", ...)` 起后台任务，返回 task_id + 计数。

    后台每个单位由 `build_greeting_unit` 驱动（包既有 apply_batch + 逐岗位先写库再发下一个）。
    `executor` / `lock` / `get_automation` / `pw_runner` / `paused` 均可注入（测试注入假件）。

    Step 5.3 DRY_RUN：提交时读全局 `dry_run` 设置并**烘焙进后台单位**（任务中途改设置不影响
    已提交任务的演练一致性）——dry_run 下工具照常提交后台任务（审批门 / 进度 / 终态全链路
    照演练），但每个单位只记"将要发送"不发浏览器；返回体带 `dry_run` 标志供 Agent 汇报。
    """
    flow = lock if lock is not None else default_flow_lock
    loader = get_automation or _default_get_automation
    runner = pw_runner or _default_pw_runner
    is_paused = paused if paused is not None else _default_paused

    def send_greetings(**kwargs: Any) -> dict:
        try:
            p = SendGreetingsParams(**kwargs)
        except ValidationError as e:
            return {"error": "参数校验失败", "message": str(e)}

        if is_paused():
            return {"error": "监控已暂停", "message": "用户已暂停监控，请恢复后再打招呼"}

        # Step 5.3：读全局 dry_run 演练开关（提交时定，烘焙进单位；安全开关 Agent 只读）
        dry_run = _get_dry_run(engine)

        progress = get_progress_factory(engine)()
        remaining = progress.get("remaining", 0) or 0
        if remaining <= 0:
            return {
                "error": "今日额度已用完",
                "message": f"今日已投 {progress.get('today_applied', 0)}，无剩余额度",
            }
        cap = min(p.max_count, remaining)

        with SASession(engine) as s:
            # V1.3.0：keyword 筛选——用户指定关键词时只投岗位名/公司命中的库存（大小写不敏感）
            filters = [models.Application.status.in_(sorted(state.JobStatus.GREETABLE))]
            kw = (p.keyword or "").strip()
            if kw:
                like = f"%{kw}%"
                filters.append(
                    models.Application.job_title.ilike(like) | models.Application.company.ilike(like)
                )
            rows = (
                s.execute(
                    select(models.Application)
                    .where(*filters)
                    .order_by(models.Application.updated_at.desc())
                    .limit(cap)
                )
                .scalars()
                .all()
            )
            jobs = [_application_job_row(r) for r in rows]
        if not jobs:
            if kw:
                return {
                    "error": "没有匹配关键词的岗位",
                    "message": f"库存里没有岗位名/公司含「{kw}」的未打招呼岗位；可先 search_jobs 搜新的，或让用户换关键词/去掉筛选",
                }
            return {"error": "没有可打招呼的岗位", "message": "仓库里没有未打招呼（pending/discovered）的岗位"}

        greeting = _resolve_greeting(engine, jobs)
        ex = executor if executor is not None else _default_executor()
        task_id = ex.submit(
            kind="send_greetings",
            total=len(jobs),
            unit_fn=build_greeting_unit(
                engine, jobs, greeting, lock=flow, get_automation=loader, pw_runner=runner, dry_run=dry_run,
            ),
            params={
                "count": len(jobs),
                "greeting": greeting,
                "keyword": kw or None,
                # 崩溃恢复（Step 4.3）用 job_urls 把 progress_done 下标映射回在途岗位，
                # 定位"发送结果未知"岗位做隔离，防止续投重复打招呼。
                "job_urls": [j["job_url"] for j in jobs],
                # Step 5.3：dry_run 标记进 params——恢复逻辑对演练任务不做 unknown 隔离
                #（演练从未实际发送，无"结果未知"岗位；见 agent/recovery.py）。
                "dry_run": dry_run,
            },
            # Step 4.4 连续失败熔断联动：单家 HR 瞬败（如页面偶发错误）不拖垮整批，但
            # 连续 3 家崩（浏览器可能卡死）即熔断停止剩余单位，防止空转整个批次。
            consecutive_fail_threshold=3,
        )
        return {
            "error": None,
            "task_id": task_id,
            "count": len(jobs),
            "keyword": kw or None,
            "remaining": remaining,
            "daily_limit": progress.get("daily_limit"),
            "effective_limit": progress.get("effective_limit"),
            "dry_run": dry_run,
        }

    return send_greetings


def build_send_tools(
    engine,
    registry: ToolRegistry | None = None,
    *,
    executor=None,
    lock: FlowLock | None = None,
    get_automation: Callable[[], Any] | None = None,
    pw_runner: Callable[..., Any] | None = None,
    paused: Callable[[], bool] | None = None,
) -> ToolRegistry:
    """注册 send_greetings 到 registry（缺省新建）。write=True → audit 模式过审批门。"""
    reg = registry or ToolRegistry()
    reg.register(
        "send_greetings",
        func=send_greetings_factory(
            engine, executor=executor, lock=lock, get_automation=get_automation, pw_runner=pw_runner, paused=paused
        ),
        description=(
            "给未打招呼的岗位批量发招呼语（写 + 后台长任务，audit 模式下挂起等确认）。"
            "提交后台任务逐岗位'先写库再发下一个'；每日上限/公司去重/HR 活跃过滤沿用 apply_batch 既有逻辑（不重写）；"
            "浏览器互斥 FlowLock，对话不阻塞，可随时查进度/停。"
            "用户指定了想投的关键词时必须传 keyword 参数（只投岗位名/公司命中的库存）；"
            "用户没说关键词时先 ask_user 反问，不要把鱼龙混杂的整批库存全投出去"
        ),
        schema=build_tool_schema("send_greetings", "给未打招呼岗位发招呼语（后台长任务，可按关键词筛选库存）", SendGreetingsParams),
        write=True,
        requires_browser=True,
        browser_ready=_ready_probe(get_automation or _default_get_automation),
    )
    return reg


__all__ = [
    "QueryJobsParams",
    "GetProgressParams",
    "UpdateSettingParams",
    "SearchJobsParams",
    "ConversationsSummaryParams",
    "SendGreetingsParams",
    "BrowserLifecycleParams",
    "open_browser_factory",
    "close_browser_factory",
    "query_jobs_factory",
    "get_progress_factory",
    "update_setting_factory",
    "search_jobs_factory",
    "get_conversations_summary_factory",
    "build_greeting_unit",
    "send_greetings_factory",
    "build_read_tools",
    "build_write_tools",
    "build_browser_tools",
    "build_send_tools",
]
