#!/usr/bin/env python3
"""
SQLite 数据层 —— 投递记录、聊天消息、设置、每日统计。
"""

import sqlite3
import threading
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).parent / ".boss_profile" / "boss_state.db"

_local = threading.local()


def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            job_url TEXT UNIQUE NOT NULL,
            city TEXT,
            experience TEXT,
            education TEXT,
            hr_name TEXT,
            hr_title TEXT,
            description TEXT,
            status TEXT DEFAULT 'pending',
            greeting_text TEXT,
            greeting_sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER REFERENCES applications(id),
            hr_name TEXT NOT NULL,
            hr_company TEXT,
            hr_title TEXT,
            job_title TEXT,
            last_message_text TEXT,
            last_message_from TEXT,
            last_message_at TIMESTAMP,
            unread_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            auto_reply_enabled INTEGER DEFAULT 1,
            interest_level TEXT,
            hr_wechat TEXT,
            wechat_shared_at TIMESTAMP,
            online_status TEXT DEFAULT '',
            resume_sent INTEGER DEFAULT 0,
            phone_shared INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            delivery_status TEXT,
            ai_generated INTEGER DEFAULT 0,
            platform_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            applications_sent INTEGER DEFAULT 0,
            messages_sent INTEGER DEFAULT 0,
            messages_received INTEGER DEFAULT 0,
            auto_replies_sent INTEGER DEFAULT 0
        );
    """)
    try:
        db.execute("ALTER TABLE messages ADD COLUMN delivery_status TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE messages ADD COLUMN platform_time TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN interest_level TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN hr_wechat TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN wechat_shared_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN resume_sent INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN phone_shared INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN online_status TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN hr_title TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN salary TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE conversations ADD COLUMN city TEXT")
    except sqlite3.OperationalError:
        pass
    # CHANGES.md §1 §4: 公司去重 + HR 活跃度列
    try:
        db.execute("ALTER TABLE applications ADD COLUMN company_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN brand_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN hr_active_label TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN hr_active_days INTEGER DEFAULT -1")
    except sqlite3.OperationalError:
        pass
    # AI 24h 缓存列 (PR #3 合并后补回)
    try:
        db.execute("ALTER TABLE applications ADD COLUMN optimize_result TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN optimize_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN chat_suggestion_result TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE applications ADD COLUMN chat_suggestion_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    # 候选池表
    db.executescript("""
        CREATE TABLE IF NOT EXISTS shortlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT UNIQUE NOT NULL,
            job_title TEXT NOT NULL,
            company TEXT,
            salary TEXT,
            city TEXT,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # CHANGES.md §3: 公司信息缓存表 (24h TTL, UNIQUE(name, company_id))
    db.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company_id TEXT,
            industry TEXT,
            scale TEXT,
            stage TEXT,
            employee_count TEXT,
            founded TEXT,
            open_positions TEXT,
            description TEXT,
            source_url TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name COLLATE NOCASE, company_id)
        );
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name COLLATE NOCASE)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_companies_fetched_at ON companies(fetched_at)")
    # 默认设置
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
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()


def _row_to_dict(row) -> Optional[dict]:
    return dict(row) if row else None


def _rows_to_list(rows) -> List[dict]:
    return [dict(r) for r in rows]


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
    """去除中英文公司后缀, 做模糊匹配.

    Examples:
        "字节跳动有限公司" -> "字节跳动"
        "阿里巴巴（中国）集团" -> "阿里巴巴"
        " 小米科技 " -> "小米科技"
    """
    if not name:
        return ""
    n = name.strip()
    for suf in _COMPANY_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)].strip()
    return n


def has_company_been_applied(company: str, company_id: str = "") -> dict:
    """检查某公司是否已投递过.

    - status in ('applied', 'replied', 'interview') 视为已发
    - pending / skipped / failed / filtered 不算
    - 精确匹配 + 用 _normalize_company_name 模糊匹配
    - company_id 非空时, 也按 company_id 精确匹配

    Returns:
        {"applied": bool, "count": int, "matched_name": str}
    """
    if not company and not company_id:
        return {"applied": False, "count": 0, "matched_name": ""}

    db = get_db()
    applied_status = ("applied", "replied", "interview")
    placeholders = ",".join("?" * len(applied_status))
    name_norm = _normalize_company_name(company)

    # 1. company_id 精确
    if company_id:
        row = db.execute(
            f"SELECT COUNT(*) as cnt, MAX(company) as name FROM applications "
            f"WHERE company_id=? AND status IN ({placeholders})",
            (company_id, *applied_status),
        ).fetchone()
        if row and row["cnt"] > 0:
            return {"applied": True, "count": row["cnt"], "matched_name": row["name"] or ""}

    # 2. 精确
    if company:
        row = db.execute(
            f"SELECT COUNT(*) as cnt FROM applications WHERE company=? AND status IN ({placeholders})",
            (company, *applied_status),
        ).fetchone()
        if row and row["cnt"] > 0:
            return {"applied": True, "count": row["cnt"], "matched_name": company}

    # 3. 模糊 (按归一化名匹配, 排除前缀冲突: 字节跳动 不匹配 字节外包)
    if name_norm and len(name_norm) >= 2:
        rows = db.execute(
            f"SELECT company, COUNT(*) as cnt FROM applications WHERE status IN ({placeholders}) GROUP BY company",
            (*applied_status,),
        ).fetchall()
        for r in rows:
            if _normalize_company_name(r["company"]) == name_norm:
                return {"applied": True, "count": r["cnt"], "matched_name": r["company"]}

    return {"applied": False, "count": 0, "matched_name": ""}


def list_applied_companies(limit: int = 200) -> List[dict]:
    """列出所有已发过的公司及最近一次投递时间.

    排除: 经验字段 (3-5年/1-3年/应届 等) 错填到 company 列的脏数据.
    """
    return _rows_to_list(
        get_db()
        .execute(
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
    d = dict(row)
    raw_positions = d.get("open_positions") or "[]"
    try:
        d["open_positions"] = json.loads(raw_positions) if isinstance(raw_positions, str) else (raw_positions or [])
    except (json.JSONDecodeError, TypeError):
        d["open_positions"] = []
    return d


def get_cached_company(name: str, company_id: str = "", max_age_hours: int = COMPANY_CACHE_TTL_HOURS) -> Optional[dict]:
    """读缓存, 过期返回 None. 默认 24h 内复用."""
    db = get_db()
    if company_id:
        row = db.execute(
            """SELECT * FROM companies
               WHERE company_id=? AND fetched_at > datetime('now', ? || ' hours')
               ORDER BY fetched_at DESC LIMIT 1""",
            (company_id, f"-{max_age_hours}"),
        ).fetchone()
        if row:
            return _company_cache_row_to_dict(row)
    if name:
        row = db.execute(
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
    """写入/刷新公司信息缓存. ON CONFLICT 走 UPSERT 路径, 自动刷新 fetched_at."""
    db = get_db()
    positions_json = json.dumps(open_positions or [], ensure_ascii=False)
    cur = db.execute(
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
    """清 N 小时前的过期缓存, 返回清理条数. 默认清 7 天前."""
    db = get_db()
    cur = db.execute(
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
    """清洗 BOSS 公司详情页'在招岗位'字段, 过滤薪资文案和 UI 噪音.

    Returns:
        (cleaned_str, count)

    Examples:
        >>> clean_open_positions("5-7K、5-10K、3-5K、职位搜索、AI Agent开发工程师、电商运营、更多")
        ('AI Agent开发工程师、电商运营', 2)
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
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", p):
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
    cur = db.execute(
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
    # 岗位列表上限 2000 条：超出时删除最旧的 pending 记录（保留 applied 等已投递状态）
    _MAX_APPLICATIONS = 2000
    total = db.execute("SELECT COUNT(*) as cnt FROM applications").fetchone()["cnt"]
    if total > _MAX_APPLICATIONS:
        excess = total - _MAX_APPLICATIONS
        db.execute(
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
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone())


def get_application_by_url(url: str) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM applications WHERE job_url=?", (url,)).fetchone())


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
    db.execute(
        f"""UPDATE applications SET {", ".join(assignments)},
            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        params,
    )
    db.commit()
    return get_application(app_id)


def list_applications(status: Optional[str] = None, limit: int = 50) -> List[dict]:
    db = get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM applications WHERE status=? ORDER BY updated_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM applications ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_list(rows)


def update_application_status(app_id: int, status: str, greeting_text: Optional[str] = None):
    db = get_db()
    if greeting_text:
        db.execute(
            """UPDATE applications SET status=?, greeting_text=?, greeting_sent_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, greeting_text, app_id),
        )
    else:
        db.execute(
            "UPDATE applications SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, app_id),
        )
    db.commit()


def get_today_application_count() -> int:
    row = (
        get_db()
        .execute("SELECT COUNT(*) as cnt FROM applications WHERE date(greeting_sent_at)=date('now','localtime')")
        .fetchone()
    )
    return row["cnt"] if row else 0


def get_today_pending_count() -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications WHERE status='pending'").fetchone()
    return row["cnt"] if row else 0


def count_filtered_applications() -> int:
    """全量统计 status='filtered' 的岗位（投递时被关键词过滤的）。"""
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications WHERE status='filtered'").fetchone()
    return row["cnt"] if row else 0


def get_total_application_count() -> int:
    """全量统计 applications 表总记录数（用于投递记录页「岗位列表」卡片）。"""
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications").fetchone()
    return row["cnt"] if row else 0


def count_applied_applications() -> int:
    """全量统计 status='applied' 的岗位（投递记录页「列表内投递」卡片）。"""
    row = get_db().execute("SELECT COUNT(*) as cnt FROM applications WHERE status='applied'").fetchone()
    return row["cnt"] if row else 0


def get_daily_limit() -> int:
    """每日投递上限，优先读 settings 表，否则取 daily_stats.daily_limit，否则兜底 15。"""
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
        .execute(
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
    return row["cnt"] if row else 0


def count_interest_level(level: str) -> int:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM conversations WHERE interest_level=?", (level,)).fetchone()
    return row["cnt"] if row else 0


def get_pending_applications(limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
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
        row = db.execute("SELECT id FROM conversations WHERE application_id=?", (application_id,)).fetchone()
        if row:
            # 更新 hr_title 如果为空
            if hr_title:
                db.execute("UPDATE conversations SET hr_title=? WHERE id=?", (hr_title, row["id"]))
                db.commit()
            return row["id"]
    # 按 HR 名字查重（精确匹配，去空白）
    name = hr_name.strip() if hr_name else ""
    if name:
        row = db.execute("SELECT id FROM conversations WHERE hr_name=? AND status!='closed'", (name,)).fetchone()
        if row:
            # 更新 hr_title 如果为空
            if hr_title:
                db.execute("UPDATE conversations SET hr_title=? WHERE id=?", (hr_title, row["id"]))
                db.commit()
            return row["id"]
    cur = db.execute(
        """INSERT INTO conversations (application_id, hr_name, hr_company, job_title, hr_title)
           VALUES (?, ?, ?, ?, ?)""",
        (application_id, name, hr_company, job_title, hr_title),
    )
    db.commit()
    return cur.lastrowid


def get_conversation(conv_id: int) -> Optional[dict]:
    return _row_to_dict(get_db().execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone())


def list_active_conversations() -> List[dict]:
    return _rows_to_list(
        get_db().execute("SELECT * FROM conversations WHERE status!='closed' ORDER BY updated_at DESC").fetchall()
    )


def find_conversation_by_hr_name(hr_name: str) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM conversations WHERE hr_name=? ORDER BY updated_at DESC LIMIT 1",
            (hr_name,),
        )
        .fetchone()
    )


def update_conversation_last_message(conv_id: int, text: str, sender: str, unread_delta: int = 0):
    """更新会话的最后一条消息摘要。

    只在消息内容或发送者真的变化时才更新 last_message_at，
    避免监控循环打开旧会话时无意义地刷新时间戳导致"收到回复"虚增。
    """
    db = get_db()
    # 先检查是否真的有变化
    current = db.execute(
        "SELECT last_message_text, last_message_from FROM conversations WHERE id=?",
        (conv_id,),
    ).fetchone()
    if current and current["last_message_text"] == text[:200] and current["last_message_from"] == sender:
        # 内容和发送者都没变，只更新 unread_count（如果有 delta）
        if unread_delta:
            db.execute(
                "UPDATE conversations SET unread_count=MAX(0, unread_count+?) WHERE id=?",
                (unread_delta, conv_id),
            )
            db.commit()
        return
    # 有变化：更新全部字段包括 last_message_at
    db.execute(
        """UPDATE conversations SET last_message_text=?, last_message_from=?,
           last_message_at=CURRENT_TIMESTAMP, unread_count=MAX(0, unread_count+?),
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (text[:200], sender, unread_delta, conv_id),
    )
    db.commit()


def update_conversation_status(conv_id: int, status: str):
    get_db().execute(
        "UPDATE conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, conv_id),
    )
    get_db().commit()


def update_conversation_interest(conv_id: int, level: str):
    get_db().execute(
        "UPDATE conversations SET interest_level=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (level, conv_id),
    )
    get_db().commit()


def update_conversation_wechat(conv_id: int, wechat_id: str):
    get_db().execute(
        "UPDATE conversations SET hr_wechat=?, wechat_shared_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (wechat_id, conv_id),
    )
    get_db().commit()


def mark_resume_sent(conv_id: int):
    get_db().execute("UPDATE conversations SET resume_sent=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def mark_phone_shared(conv_id: int):
    get_db().execute("UPDATE conversations SET phone_shared=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
    get_db().commit()


def get_wechat_exchanges() -> List[dict]:
    """返回所有已获取到微信号的会话，包含岗位详情。"""
    return _rows_to_list(
        get_db()
        .execute(
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
    get_db().execute(
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
    cur = db.execute(
        "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, sender, content, delivery_status, 1 if ai_generated else 0),
    )
    db.commit()
    return cur.lastrowid


def get_messages(conversation_id: int, limit: int = 50) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def get_recent_messages(conversation_id: int, limit: int = 5) -> List[dict]:
    return _rows_to_list(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (conversation_id, limit),
        )
        .fetchall()
    )


def replace_conversation_messages(conversation_id: int, messages: List[dict]):
    """用 BOSS 当前消息历史覆盖本地缓存，避免 Web 端展示过期或错会话内容。"""
    db = get_db()
    old_ai = {
        r["content"]
        for r in db.execute(
            "SELECT content FROM messages WHERE conversation_id=? AND ai_generated=1",
            (conversation_id,),
        ).fetchall()
    }
    db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    for msg in messages:
        sender = msg.get("sender", "hr")
        content = (msg.get("content") or "").strip()
        delivery_status = (msg.get("status") or msg.get("delivery_status") or "").strip()
        platform_time = (msg.get("time") or "").strip() or None
        if not content:
            continue
        ai_generated = 1 if sender == "me" and content in old_ai else 0
        db.execute(
            "INSERT INTO messages (conversation_id, sender, content, delivery_status, ai_generated, platform_time) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, sender, content, delivery_status, ai_generated, platform_time),
        )
    db.commit()
    # 更新会话的 last_message_at 为最新的平台时间（如果有）
    if messages:
        last = messages[-1]
        last_time = (last.get("time") or "").strip()
        if last_time:
            try:
                db.execute(
                    "UPDATE conversations SET last_message_at=? WHERE id=?",
                    (last_time, conversation_id),
                )
                db.commit()
            except Exception:
                pass


def get_last_hr_message(conversation_id: int) -> Optional[dict]:
    return _row_to_dict(
        get_db()
        .execute(
            "SELECT * FROM messages WHERE conversation_id=? AND sender='hr' ORDER BY created_at DESC LIMIT 1",
            (conversation_id,),
        )
        .fetchone()
    )


def message_exists(conversation_id: int, content: str, sender: str) -> bool:
    row = (
        get_db()
        .execute(
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
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    get_db().execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    get_db().commit()


def get_all_settings() -> dict:
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ══════════════════════════════════════
#  Daily Stats
# ══════════════════════════════════════


def _today() -> str:
    return date.today().isoformat()


def _ensure_today():
    get_db().execute("INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (_today(),))
    get_db().commit()


def increment_daily_stat(field: str):
    _ensure_today()
    get_db().execute(
        f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date=?",
        (_today(),),
    )
    get_db().commit()


def get_daily_stats(date_str: Optional[str] = None) -> dict:
    d = date_str or _today()
    row = get_db().execute("SELECT * FROM daily_stats WHERE date=?", (d,)).fetchone()
    return dict(row) if row else {}


def get_today_auto_reply_count() -> int:
    row = (
        get_db()
        .execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE ai_generated=1 AND date(created_at)=date('now','localtime')"
        )
        .fetchone()
    )
    return row["cnt"] if row else 0


# ═══════════════════════
#  候选池
# ═══════════════════════
def add_to_shortlist(
    job_url: str, title: str, company: str = "", salary: str = "", city: str = "", note: str = ""
) -> int:
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO shortlists (job_url, job_title, company, salary, city, note) VALUES (?,?,?,?,?,?)",
            (job_url, title, company, salary, city, note),
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return 0


def remove_from_shortlist(shortlist_id: int):
    get_db().execute("DELETE FROM shortlists WHERE id=?", (shortlist_id,))
    get_db().commit()


def list_shortlists(limit: int = 100) -> list:
    rows = get_db().execute("SELECT * FROM shortlists ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return _rows_to_list(rows)


def is_in_shortlist(job_url: str) -> bool:
    row = get_db().execute("SELECT COUNT(*) as cnt FROM shortlists WHERE job_url=?", (job_url,)).fetchone()
    return row["cnt"] > 0 if row else False


def clear_all_applications() -> int:
    """清空所有岗位列表（applications + shortlists），返回删除行数。"""
    db = get_db()
    app_count = db.execute("SELECT COUNT(*) as cnt FROM applications").fetchone()["cnt"]
    short_count = db.execute("SELECT COUNT(*) as cnt FROM shortlists").fetchone()["cnt"]
    db.execute("DELETE FROM applications")
    db.execute("DELETE FROM shortlists")
    db.commit()
    return app_count + short_count


def clear_all_conversations() -> int:
    """清空所有聊天数据（conversations + messages），返回删除行数。"""
    db = get_db()
    conv_count = db.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()["cnt"]
    db.execute("DELETE FROM messages")
    db.execute("DELETE FROM conversations")
    db.commit()
    return conv_count


# 启动时初始化
init_db()
