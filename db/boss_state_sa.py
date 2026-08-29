"""Step 1.2 适配层：以 SQLAlchemy 引擎 + `db.models`(11 表) 复刻 `boss_state` 全部公开函数，行为逐字对齐。

- 复用 `db.base.get_engine()`（SQLite WAL + FK）与 `db.models`（Step 1.1 底座）。
- 用 `connection.exec_driver_sql("…?", params)` 原样执行存量 SQL（`?` 位置占位，DBAPI
  层即 sqlite3），最大化逐字对齐、最小化转移风险。
- 每线程持有单个连接（`threading.local`，与存量 `get_db` 模型一致），写操作显式 `commit()`。
- `_reset()` 为测试隔离入口：关闭并丢弃当前线程连接，下次调用按新 `AI_PLATFORM_DB` 重建。
- 与存量的刻意差异：写失败时显式回滚（如候选池重复插入返回 0），比存量更稳。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from typing import List, Optional

import sqlalchemy as sa

from db import models
from db.base import get_db_path, get_engine

# 与存量 `boss_state.DB_PATH` 对齐的公开常量（DB 文件路径）。
DB_PATH = get_db_path()

_local = threading.local()


def _reset() -> None:
    """关闭并丢弃当前线程已缓存的连接；测试隔离用（内部函数）。"""
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


def get_db():
    """返回当前线程的 SQLAlchemy 连接（懒建，WAL/FK 由引擎 connect 事件设置）。"""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = get_engine().connect()
    return _local.conn


def init_db():
    """建表（若缺）+ 灌默认设置。与存量 init_db 语义一致。"""
    get_engine()  # 确保目录存在
    models.Base.metadata.create_all(get_engine())
    db = get_db()
    defaults = {
        "greeting_template": "您好！看到贵司在招{job_title}，挺感兴趣的。PS：正在和你聊天的这个AI工具是我自己开发的——就当是我的技术名片了",
        "greeting_mode": "template",
        "smart_greeting_prompt": "",
        "greeting_enabled": "true",
        "ai_reply_style": "professional",
        "daily_apply_limit": "15",
        "auto_reply_enabled": "false",
        "min_reply_delay_sec": "15",
        "max_reply_delay_sec": "20",
        "batch_delay_min_sec": "30",
        "batch_delay_max_sec": "90",
        "resume_summary": "",
        "wechat_id": "",
        "search_keywords": "",
        "default_city": "全国",
        "max_hr_inactive_days": "7",
        "filter_inactive_hr": "true",
        "dedup_company_by_default": "true",
    }
    for k, v in defaults.items():
        db.exec_driver_sql("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()


def _row_to_dict(row) -> Optional[dict]:
    return dict(row._mapping) if row is not None else None


def _rows_to_list(rows) -> List[dict]:
    return [dict(r._mapping) for r in rows]


# ══════════════════════════════════════
#  公司去重 (CHANGES §1)
# ══════════════════════════════════════

_COMPANY_SUFFIXES = (
    "有限公司",
    "有限责任公司",
    "股份有限公司",
    "集团",
    "集团有限",
    "(中国)",
    "（中国）",
    "股份",
)


def _normalize_company_name(name: str) -> str:
    """去除中英文公司后缀, 做模糊匹配。"""
    if not name:
        return ""
    n = name.strip()
    for suf in _COMPANY_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
    return n


def has_company_been_applied(company: str, company_id: str = "") -> dict:
    if not company and not company_id:
        return {"applied": False, "count": 0, "matched_name": ""}

    db = get_db()
    applied_status = ("applied", "replied", "interview")
    placeholders = ",".join("?" * len(applied_status))
    name_norm = _normalize_company_name(company)

    if company_id:
        row = db.exec_driver_sql(
            f"SELECT COUNT(*) as cnt, MAX(company) as name FROM applications "
            f"WHERE company_id=? AND status IN ({placeholders})",
            (company_id, *applied_status),
        ).fetchone()
        if row and row._mapping["cnt"] > 0:
            return {"applied": True, "count": row._mapping["cnt"], "matched_name": row._mapping["name"] or ""}

    if company:
        row = db.exec_driver_sql(
            f"SELECT COUNT(*) as cnt FROM applications WHERE company=? AND status IN ({placeholders})",
            (company, *applied_status),
        ).fetchone()
        if row and row._mapping["cnt"] > 0:
            return {"applied": True, "count": row._mapping["cnt"], "matched_name": company}

    if name_norm and len(name_norm) >= 2:
        rows = db.exec_driver_sql(
            f"SELECT company, COUNT(*) as cnt FROM applications WHERE status IN ({placeholders}) GROUP BY company",
            (*applied_status,),
        ).fetchall()
        for r in rows:
            if _normalize_company_name(r._mapping["company"]) == name_norm:
                return {"applied": True, "count": r._mapping["cnt"], "matched_name": r._mapping["company"]}

    return {"applied": False, "count": 0, "matched_name": ""}


def list_applied_companies(limit: int = 200) -> List[dict]:
    """列出所有已发过的公司及最近一次投递时间（排除经验脏数据）。"""
    return _rows_to_list(
        get_db()
        .exec_driver_sql(
            """SELECT company, COUNT(*) as applied_count, MAX(updated_at) as last_applied_at
               FROM applications
               WHERE company IS NOT NULL AND company != ''
                 AND length(company) >= 2 AND length(company) <= 40
                 AND company NOT GLOB '*[0-9]年*'
                 AND company NOT GLOB '*经验*'
                 AND company NOT GLOB '*学历*'
                 AND company NOT GLOB '*应届*'
                 AND company NOT IN ('中专/中技','高中','大专','本科','硕士','博士','学历不限')
                 AND status IN ('applied', 'replied', 'interview')
               GROUP BY company COLLATE NOCASE
               ORDER BY last_applied_at DESC
               LIMIT ?""",
            (limit,),
        )
        .fetchall()
    )


# ══════════════════════════════════════
#  公司信息缓存 (CHANGES §3, 24h TTL)
# ══════════════════════════════════════

COMPANY_CACHE_TTL_HOURS = 24


def _company_cache_row_to_dict(row) -> Optional[dict]:
    if not row:
        return None
    d = dict(row._mapping)
    raw_positions = d.get("open_positions") or "[]"
    try:
        d["open_positions"] = json.loads(raw_positions) if isinstance(raw_positions, str) else (raw_positions or [])
    except (json.JSONDecodeError, TypeError):
        d["open_positions"] = []
    return d


def get_cached_company(name: str, company_id: str = "", max_age_hours: int = COMPANY_CACHE_TTL_HOURS) -> Optional[dict]:
    db = get_db()
    if company_id:
        row = db.exec_driver_sql(
            """SELECT * FROM companies
               WHERE company_id=? AND fetched_at > datetime('now', ? || ' hours')
               ORDER BY fetched_at DESC LIMIT 1""",
            (company_id, f"-{max_age_hours}"),
        ).fetchone()
        if row:
            return _company_cache_row_to_dict(row)
    if name:
        row = db.exec_driver_sql(
            """SELECT * FROM companies
               WHERE name=? COLLATE NOCASE AND fetched_at > datetime('now', ? || ' hours')
               ORDER BY fetched_at DESC LIMIT 1""",
            (name, f"-{max_age_hours}"),
        ).fetchone()
        if row:
            return _company_cache_row_to_dict(row)
    return None


def save_company_cache(
    name: str,
    company_id: str = "",
    industry: str = "",
    scale: str = "",
    stage: str = "",
    employee_count: str = "",
    founded: str = "",
    open_positions: Optional[List[str]] = None,
    description: str = "",
    source_url: str = "",
) -> int:
    db = get_db()
    positions_json = json.dumps(open_positions or [], ensure_ascii=False)
    cur = db.exec_driver_sql(
        """INSERT INTO companies
           (name, company_id, industry, scale, stage, employee_count, founded,
            open_positions, description, source_url, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(name COLLATE NOCASE, company_id) DO UPDATE SET
             industry=excluded.industry,
             scale=excluded.scale,
             stage=excluded.stage,
             employee_count=excluded.employee_count,
             founded=excluded.founded,
             open_positions=excluded.open_positions,
             description=excluded.description,
             source_url=excluded.source_url,
             fetched_at=CURRENT_TIMESTAMP""",
        (
            name,
            company_id or "",
            industry,
            scale,
            stage,
            employee_count,
            founded,
            positions_json,
            description,
            source_url,
        ),
    )
    db.commit()
    return cur.lastrowid


def list_companies_for_cleanup(older_than_hours: int = 168) -> int:
    db = get_db()
    cur = db.exec_driver_sql(
        "DELETE FROM companies WHERE fetched_at < datetime('now', ? || ' hours')",
        (f"-{older_than_hours}",),
    )
    db.commit()
    return cur.rowcount


# ══════════════════════════════════════
#  公司在招岗位清理 (辅助 _scrape_company_page 过滤脏数据)
# ══════════════════════════════════════

_NOISE_POSITIONS = {
    "更多",
    "查看更多",
    "全部",
    "收起",
    "展开",
    "加载更多",
    "职位搜索",
    "搜索",
    "热门",
    "推荐",
}

_SALARY_PAT = re.compile(r"(\d+\s*[-~到至]?\s*\d*\s*[Kk万])|(\d+\s*元/?月)")


def clean_open_positions(raw):
    """清洗 BOSS 公司详情页'在招岗位'字段, 过滤薪资文案和 UI 噪音。

    Returns:
        (cleaned_str, count)
    """
    if not raw:
        return ("", 0)
    parts = [p.strip() for p in re.split(r"、|,|;|/|\n", raw) if p and p.strip()]
    valid = []
    for p in parts:
        if p in _NOISE_POSITIONS:
            continue
        if _SALARY_PAT.search(p):
            continue
        if len(p) < 2 or len(p) > 40:
            continue
        if not re.search(r"[一-鿿A-Za-z]", p):
            continue
        valid.append(p)
    return ("、".join(valid), len(valid))


# ══════════════════════════════════════
#  Applications
# ══════════════════════════════════════


def add_application(job: dict) -> int:
    db = get_db()
    hr_active_days = job.get("hr_active_days")
    if hr_active_days is None or hr_active_days == "":
        hr_active_days = -1
    cur = db.exec_driver_sql(
        """INSERT OR IGNORE INTO applications
           (job_title, company, salary, job_url, city, experience, education,
            hr_name, hr_title, description,
            company_id, brand_name, hr_active_label, hr_active_days)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.get("title", ""),
            job.get("company", ""),
            job.get("salary", ""),
            job.get("url", ""),
            job.get("city", ""),
            job.get("experience", ""),
            job.get("education", ""),
            job.get("hr_name", ""),
            job.get("hr_title", ""),
            job.get("description", ""),
            job.get("company_id", ""),
            job.get("brand_name", ""),
            job.get("hr_active_label", ""),
            hr_active_days,
        ),
    )
    db.commit()
    _MAX_APPLICATIONS = 2000
    total = db.exec_driver_sql("SELECT COUNT(*) as cnt FROM applications").fetchone()._mapping["cnt"]
    if total > _MAX_APPLICATIONS:
        excess = total - _MAX_APPLICATIONS
        db.exec_driver_sql(
            """DELETE FROM applications WHERE id IN (
                SELECT id FROM applications
                WHERE status='pending'
                ORDER BY created_at ASC
                LIMIT ?
            )""",
            (excess,),
        )
        db.commit()
    return cur.lastrowid if cur.lastrowid else 0


def get_application(app_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().exec_driver_sql("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def get_application_by_url(url: str) -> Optional[dict]:
    return _row_to_dict(get_db().exec_driver_sql("SELECT * FROM applications WHERE job_url=?", (url,)).fetchone())


def update_application_from_job(app_id: int, job: dict) -> Optional[dict]:
    """用本次搜索结果刷新已有岗位；空值不覆盖旧值。"""
    fields = {
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "salary": job.get("salary", ""),
        "city": job.get("city", ""),
        "experience": job.get("experience", ""),
        "education": job.get("education", ""),
        "hr_name": job.get("hr_name", ""),
        "hr_title": job.get("hr_title", ""),
        "description": job.get("description", ""),
    }
    params = []
    assignments = []
    for column, value in fields.items():
        value = (value or "").strip()
        assignments.append(f"{column}=CASE WHEN ?!='' THEN ? ELSE {column} END")
        params.extend([value, value])
    params.append(app_id)

    db = get_db()
    db.exec_driver_sql(
        f"""UPDATE applications SET {", ".join(assignments)},
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        tuple(params),  # 扁平 list 会被 SQLAlchemy 当成 executemany；tuple 才是单组参数
    )
    db.commit()
    return get_application(app_id)


def list_applications(status: Optional[str] = None, limit: int = 50) -> List[dict]:
    db = get_db()
    if status:
        rows = db.exec_driver_sql(
            "SELECT * FROM applications WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = db.exec_driver_sql(
            "SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return _rows_to_list(rows)


def update_application_status(app_id: int, status: str, greeting_text: Optional[str] = None):
    db = get_db()
    if greeting_text:
        db.exec_driver_sql(
            """UPDATE applications SET status=?, greeting_text=?, greeting_sent_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, greeting_text, app_id),
        )
    else:
        db.exec_driver_sql(
            "UPDATE applications SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, app_id),
        )
    db.commit()


def get_today_application_count() -> int:
    row = (
        get_db()
        .exec_driver_sql(
            "SELECT COUNT(*) as cnt FROM applications WHERE date(greeting_sent_at)=date('now','localtime')"
        )
        .fetchone()
    )
    return row._mapping["cnt"] if row else 0


def get_today_pending_count() -> int:
    row = get_db().exec_driver_sql("SELECT COUNT(*) as cnt FROM applications WHERE status='pending'").fetchone()
    return row._mapping["cnt"] if row else 0


def count_filtered_applications() -> int:
    """全量统计 status='filtered' 的岗位（投递时被关键词过滤的）。"""
    row = get_db().exec_driver_sql("SELECT COUNT(*) as cnt FROM applications WHERE status='filtered'").fetchone()
    return row._mapping["cnt"] if row else 0


def get_total_application_count() -> int:
    """全量统计 applications 表总记录数（投递记录页「岗位列表」卡片）。"""
    row = get_db().exec_driver_sql("SELECT COUNT(*) as cnt FROM applications").fetchone()
    return row._mapping["cnt"] if row else 0


def count_applied_applications() -> int:
    """全量统计 status='applied' 的岗位（投递记录页「列表内投递」卡片）。"""
    row = get_db().exec_driver_sql("SELECT COUNT(*) as cnt FROM applications WHERE status='applied'").fetchone()
    return row._mapping["cnt"] if row else 0


def get_daily_limit() -> int:
    """每日投递上限，优先读 settings 表，否则兜底 15。"""
    try:
        v = get_setting("daily_apply_limit")
        if v:
            return int(v)
    except Exception:
        pass
    return 15


def count_hours_replied_in_range(hours: int) -> int:
    row = (
        get_db()
        .exec_driver_sql(
            """SELECT COUNT(*) as cnt FROM conversations
               WHERE last_message_from='hr'
               AND datetime(COALESCE(
                   (SELECT platform_time FROM messages WHERE conversation_id=conversations.id AND sender='hr' ORDER BY id DESC LIMIT 1),
                   last_message_at
               )) > datetime('now','localtime',? || ' hours')""",
            (f"-{hours}",),
        )
        .fetchone()
    )
    return row._mapping["cnt"] if row else 0


def count_interest_level(level: str) -> int:
    row = get_db().exec_driver_sql(
        "SELECT COUNT(*) as cnt FROM conversations WHERE interest_level=?", (level,)
    ).fetchone()
    return row._mapping["cnt"] if row else 0


def get_pending_applications(limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .exec_driver_sql(
            "SELECT * FROM applications WHERE status='pending' AND job_url!='' ORDER BY id LIMIT ?",
            (limit,),
        )
        .fetchall()
    )


# ══════════════════════════════════════
#  Conversations
# ══════════════════════════════════════


def get_or_create_conversation(
    application_id: int, hr_name: str, hr_company: str, job_title: str, hr_title: str = ""
) -> int:
    db = get_db()
    if application_id:
        row = db.exec_driver_sql("SELECT id FROM conversations WHERE application_id=?", (application_id,)).fetchone()
        if row:
            if hr_title:
                db.exec_driver_sql("UPDATE conversations SET hr_title=? WHERE id=?", (hr_title, row._mapping["id"]))
                db.commit()
            return row._mapping["id"]
    name = hr_name.strip() if hr_name else ""
    if name:
        row = db.exec_driver_sql(
            "SELECT id FROM conversations WHERE hr_name=? AND status!='closed'", (name,)
        ).fetchone()
        if row:
            if hr_title:
                db.exec_driver_sql(
                    "UPDATE conversations SET hr_title=? WHERE id=?", (hr_title, row._mapping["id"])
                )
                db.commit()
            return row._mapping["id"]
    cur = db.exec_driver_sql(
        """INSERT INTO conversations (application_id, hr_name, hr_company, job_title, hr_title)
           VALUES (?, ?, ?, ?, ?)""",
        (application_id, name, hr_company, job_title, hr_title),
    )
    db.commit()
    return cur.lastrowid


def get_conversation(conv_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().exec_driver_sql("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())


def list_active_conversations() -> List[dict]:
    return _rows_to_list(
        get_db()
        .exec_driver_sql("SELECT * FROM conversations WHERE status!='closed' ORDER BY updated_at DESC")
        .fetchall()
    )


def find_conversation_by_hr_name(hr_name: str) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .exec_driver_sql(
            "SELECT * FROM conversations WHERE hr_name=? ORDER BY updated_at DESC LIMIT 1",
            (hr_name,),
        )
        .fetchone()
    )


def update_conversation_last_message(conv_id: int, text: str, sender: str, unread_delta: int = 0):
    """更新会话的最后一条消息摘要；内容/发送者未变则不刷新 last_message_at。"""
    db = get_db()
    current = db.exec_driver_sql(
        "SELECT last_message_text, last_message_from FROM conversations WHERE id=?",
        (conv_id,),
    ).fetchone()
    if current and current._mapping["last_message_text"] == text[:200] and current._mapping["last_message_from"] == sender:
        if unread_delta:
            db.exec_driver_sql(
                "UPDATE conversations SET unread_count=MAX(0, unread_count+?) WHERE id=?",
                (unread_delta, conv_id),
            )
            db.commit()
        return
    db.exec_driver_sql(
        """UPDATE conversations SET last_message_text=?, last_message_from=?,
           last_message_at=CURRENT_TIMESTAMP, unread_count=MAX(0, unread_count+?),
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (text[:200], sender, unread_delta, conv_id),
    )
    db.commit()


def update_conversation_status(conv_id: int, status: str):
    get_db().exec_driver_sql(
        "UPDATE conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, conv_id),
    )
    get_db().commit()


def update_conversation_interest(conv_id: int, level: str):
    get_db().exec_driver_sql(
        "UPDATE conversations SET interest_level=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (level, conv_id),
    )
    get_db().commit()


def update_conversation_wechat(conv_id: int, wechat_id: str):
    get_db().exec_driver_sql(
        "UPDATE conversations SET hr_wechat=?, wechat_shared_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (wechat_id, conv_id),
    )
    get_db().commit()


def mark_resume_sent(conv_id: int):
    get_db().exec_driver_sql(
        "UPDATE conversations SET resume_sent=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,)
    )
    get_db().commit()


def mark_phone_shared(conv_id: int):
    get_db().exec_driver_sql(
        "UPDATE conversations SET phone_shared=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,)
    )
    get_db().commit()


def get_wechat_exchanges() -> List[dict]:
    return _rows_to_list(
        get_db()
        .exec_driver_sql(
            """SELECT c.id, c.hr_name, c.hr_company, c.job_title, c.hr_wechat,
                      c.wechat_shared_at, c.interest_level,
                      a.city, a.salary, a.experience, a.education, a.description
               FROM conversations c
               LEFT JOIN applications a ON c.application_id = a.id
               WHERE c.hr_wechat IS NOT NULL AND c.hr_wechat != ''
               ORDER BY c.wechat_shared_at DESC"""
        )
        .fetchall()
    )


def set_auto_reply(conv_id: int, enabled: bool):
    get_db().exec_driver_sql(
        "UPDATE conversations SET auto_reply_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (1 if enabled else 0, conv_id),
    )
    get_db().commit()


# ══════════════════════════════════════
#  Messages
# ══════════════════════════════════════


def add_message(
    conversation_id: int, sender: str, content: str, ai_generated: bool = False, delivery_status: str = ""
) -> int:
    db = get_db()
    cur = db.exec_driver_sql(
        "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, sender, content, delivery_status, 1 if ai_generated else 0),
    )
    db.commit()
    return cur.lastrowid


def get_messages(conversation_id: int, limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .exec_driver_sql(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def get_recent_messages(conversation_id: int, limit: int = 5) -> List[dict]:
    return _rows_to_list(
        get_db()
        .exec_driver_sql(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def replace_conversation_messages(conversation_id: int, messages: List[dict]):
    """用 BOSS 当前消息历史覆盖本地缓存，避免 Web 端展示过期或错会话内容。"""
    db = get_db()
    old_ai = {
        r._mapping["content"]
        for r in db.exec_driver_sql(
            "SELECT content FROM messages WHERE conversation_id=? AND ai_generated=1",
            (conversation_id,),
        ).fetchall()
    }
    db.exec_driver_sql("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    for msg in messages:
        sender = msg.get("sender", "hr")
        content = (msg.get("content") or "").strip()
        delivery_status = (msg.get("status") or msg.get("delivery_status") or "").strip()
        platform_time = (msg.get("time") or "").strip() or None
        if not content:
            continue
        ai_generated = 1 if sender == "me" and content in old_ai else 0
        db.exec_driver_sql(
            "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated, platform_time) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, sender, content, delivery_status, ai_generated, platform_time),
        )
    db.commit()
    if messages:
        last = messages[-1]
        last_time = (last.get("time") or "").strip()
        if last_time:
            try:
                db.exec_driver_sql(
                    "UPDATE conversations SET last_message_at=? WHERE id=?",
                    (last_time, conversation_id),
                )
                db.commit()
            except Exception:
                pass


def get_last_hr_message(conversation_id: int) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .exec_driver_sql(
            "SELECT * FROM messages WHERE conversation_id=? AND sender='hr' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        .fetchone()
    )


def message_exists(conversation_id: int, content: str, sender: str) -> bool:
    row = (
        get_db()
        .exec_driver_sql(
            "SELECT id FROM messages WHERE conversation_id=? AND content=? AND sender=? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, content, sender),
        )
        .fetchone()
    )
    return row is not None


# ══════════════════════════════════════
#  Settings
# ══════════════════════════════════════


def get_setting(key: str, default: str = "") -> str:
    row = get_db().exec_driver_sql("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row._mapping["value"] if row else default


def set_setting(key: str, value: str):
    get_db().exec_driver_sql(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    get_db().commit()


def get_all_settings() -> dict:
    rows = get_db().exec_driver_sql("SELECT key, value FROM settings").fetchall()
    return {r._mapping["key"]: r._mapping["value"] for r in rows}


# ══════════════════════════════════════
#  Daily Stats
# ══════════════════════════════════════


def _today() -> str:
    return date.today().isoformat()


def _ensure_today():
    get_db().exec_driver_sql("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (_today(),))
    get_db().commit()


def increment_daily_stat(field: str):
    _ensure_today()
    get_db().exec_driver_sql(
        f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date=?",
        (_today(),),
    )
    get_db().commit()


def get_daily_stats(date_str: Optional[str] = None) -> dict:
    d = date_str or _today()
    row = get_db().exec_driver_sql("SELECT * FROM daily_stats WHERE date=?", (d,)).fetchone()
    return dict(row._mapping) if row else {}


def get_today_auto_reply_count() -> int:
    row = (
        get_db()
        .exec_driver_sql(
            "SELECT COUNT(*) as cnt FROM messages WHERE ai_generated=1 AND date(created_at)=date('now','localtime')"
        )
        .fetchone()
    )
    return row._mapping["cnt"] if row else 0


# ══════════════════════════════════════
#  候选池
# ══════════════════════════════════════
def add_to_shortlist(
    job_url: str, title: str, company: str = "", salary: str = "", city: str = "", note: str = ""
) -> int:
    db = get_db()
    try:
        cur = db.exec_driver_sql(
            "INSERT INTO shortlists (job_url, job_title, company, salary, city, note) VALUES (?,?,?,?,?,?)",
            (job_url, title, company, salary, city, note),
        )
        db.commit()
        return cur.lastrowid
    except sa.exc.IntegrityError:
        db.rollback()
        return 0


def remove_from_shortlist(shortlist_id: int):
    get_db().exec_driver_sql("DELETE FROM shortlists WHERE id=?", (shortlist_id,))
    get_db().commit()


def list_shortlists(limit: int = 100) -> list:
    rows = get_db().exec_driver_sql(
        "SELECT * FROM shortlists ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return _rows_to_list(rows)


def is_in_shortlist(job_url: str) -> bool:
    row = get_db().exec_driver_sql(
        "SELECT COUNT(*) as cnt FROM shortlists WHERE job_url=?", (job_url,)
    ).fetchone()
    return row._mapping["cnt"] > 0 if row else False


def clear_all_applications() -> int:
    """清空所有岗位列表（applications + shortlists），返回删除行数。"""
    db = get_db()
    app_count = db.exec_driver_sql("SELECT COUNT(*) as cnt FROM applications").fetchone()._mapping["cnt"]
    short_count = db.exec_driver_sql("SELECT COUNT(*) as cnt FROM shortlists").fetchone()._mapping["cnt"]
    db.exec_driver_sql("DELETE FROM applications")
    db.exec_driver_sql("DELETE FROM shortlists")
    db.commit()
    return app_count + short_count


def clear_all_conversations() -> int:
    """清空所有聊天数据（conversations + messages），返回删除行数。"""
    db = get_db()
    conv_count = db.exec_driver_sql("SELECT COUNT(*) as cnt FROM conversations").fetchone()._mapping["cnt"]
    db.exec_driver_sql("DELETE FROM messages")
    db.exec_driver_sql("DELETE FROM conversations")
    db.commit()
    return conv_count


# ══════════════════════════════════════
#  公司画像（boss_company 专用）
# ══════════════════════════════════════
def list_companies_by_position_count(min_count: int = 1, limit: int = 50) -> list:
    """按公司聚合，统计 distinct job_url 数倒序，返回 [{company, company_id, position_count, latest_job_id}]。
    company_id 为空的公司会被单独归到 (company, '')。"""
    rows = get_db().exec_driver_sql(
        """SELECT company, company_id, COUNT(DISTINCT job_url) AS position_count, MAX(id) AS latest_job_id
           FROM applications
           WHERE company != '' AND job_url != ''
           GROUP BY company, COALESCE(NULLIF(company_id, ''), company)
           HAVING position_count >= ?
           ORDER BY position_count DESC, latest_job_id DESC
           LIMIT ?""",
        (min_count, limit),
    ).fetchall()
    return _rows_to_list(rows)


def list_jobs_by_company(company_id: str = "", company: str = "") -> list:
    """按 company_id 或 company 名返回该公司下所有已入库的岗位。
    优先用 company_id；为空时用 company 名兜底。"""
    db = get_db()
    if company_id:
        rows = db.exec_driver_sql(
            "SELECT * FROM applications WHERE company_id=? ORDER BY id DESC",
            (company_id,),
        ).fetchall()
        if rows:
            return _rows_to_list(rows)
    if company:
        return _rows_to_list(
            db.exec_driver_sql(
                "SELECT * FROM applications WHERE company=? ORDER BY id DESC",
                (company,),
            ).fetchall()
        )
    return []
