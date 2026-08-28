"""JSON envelope formatter — stdout always structured JSON."""

import json
import sys
from typing import Optional

# Windows 默认 GBK 控制台无法编码 BOSS 返回里的部分罕见 Unicode（如 \u2f24 等 CJK 部首字符）。
# 强制把 stdout/stderr 切换到 UTF-8 + 错误回退到 backslashreplace，避免 UnicodeEncodeError 中断 JSON 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[attr-defined]
    except Exception:
        pass


def ok(command: str, data=None, **kwargs) -> dict:
    envelope = {
        "ok": True,
        "command": command,
        "data": data,
        "error": None,
    }
    envelope.update(kwargs)
    return envelope


def fail(command: str, error: str) -> dict:
    return {
        "ok": False,
        "command": command,
        "data": None,
        "error": error,
    }


def emit(result: dict):
    try:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    except UnicodeEncodeError:
        # 终极兜底：转义所有非 ASCII，保证 JSON 输出永远不中断
        json.dump(result, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")


def ok_or_fail(resp, command: str) -> dict:
    """Converts an httpx response to envelope. Handles HTTP errors."""
    if resp.is_error:
        return fail(command, f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except Exception:
        return fail(command, f"invalid JSON: {resp.text[:200]}")
    if isinstance(body, dict) and body.get("detail"):
        return fail(command, str(body["detail"]))
    return ok(command, data=body)
