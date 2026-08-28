"""
面试问答Agent - 数据库配置（统一从环境变量读取，不硬编码密码）
"""
import os
import pymysql


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


DB_CONFIG = {
    "host": os.environ.get("INTERVIEW_DB_HOST", "127.0.0.1"),
    "port": _get_int("INTERVIEW_DB_PORT", 3306),
    "user": os.environ.get("INTERVIEW_DB_USER", "root"),
    "password": os.environ.get("INTERVIEW_DB_PASSWORD", ""),
    "database": os.environ.get("INTERVIEW_DB_NAME", "ai_jobs_db"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

# 别名：保留旧模块的 DB 引用
DB = DB_CONFIG


def get_conn():
    if not DB_CONFIG["password"]:
        raise RuntimeError(
            "未设置 INTERVIEW_DB_PASSWORD 环境变量。\n"
            "请复制 .env.example 为 .env 并填入密码，或：\n"
            "  export INTERVIEW_DB_PASSWORD=你的MySQL密码"
        )
    return pymysql.connect(**DB_CONFIG)
