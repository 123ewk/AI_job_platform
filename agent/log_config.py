"""结构化 JSON 日志基线与敏感信息脱敏（SDD §4.7 / Step 0.3）。

设计要点
--------
- 脱敏为**纯函数**（`mask_text` / `mask_value`），Formatter / Filter 只做包装，
  以便单元测试直接断言，且**不触碰全局 root logger**（避免重包包死 pytest 捕获）。
- 每条日志可携带结构化字段 `session_id / task_id / tool`，经 `extra` 传入；
  `JsonFormatter.format` 将其并入 JSON 顶层键。
- 掩码规则（见 `mask_text`）：
  · 大陆手机号（11 位，1[3-9] 开头）→ `138****5678`
  · `api_key` / `sk-`/`Bearer` 开头 token / `AI_API_KEY` 环境变量名 → 仅留首尾
  · `wechat_id` / `wxid` → 全掩 `***`
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

__all__ = [
    "mask_text",
    "mask_value",
    "JsonFormatter",
    "SensitiveDataFilter",
    "build_logger",
]

_STRUCT_FIELDS = ("session_id", "task_id", "tool")

# LogRecord 标准实例属性（在 __init__ 里按实例设置，非类属性，单独列出以便排除）。
_STD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)

# 大陆手机号：1 开头，第二位 3-9，共 11 位。脱敏保留前 3 后 4。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# sk- 前缀 token（DeepSeek/OpenAI/可控）——可能独立出现，也可能跟 mark 后。
_SK_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{6,}\b)")

# 键值对形式：`<key>: <value>` / `<key>=<value>`，键名含 key/token/secret/password/bearer。
_KEYVALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|ai[_-]?api[_-]?key|token|secret|password|bearer)\b"
    r"\s*[:=]\s*([A-Za-z0-9_.\-=+/]{8,})\b"
)

# Bearer 认证：`Bearer <token>`（无冒号）。
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_.\-]{8,})\b")

# wechat 值：`wechat_id / wxid / wechat = <value>` 全掩。
_WECHAT_RE = re.compile(r"(?i)\b(wechat[_-]?id|wechat|wxid|wx[_-]?id)\b\s*[:=]\s*([A-Za-z0-9_.\-]{4,})\b")


def _mask_token(token: str) -> str:
    """密钥类 token 掩码：仅保留前 4 后 3，中间用 ***** 代替。"""
    if len(token) <= 7:
        return "***"
    return f"{token[:4]}*****{token[-3:]}"


def mask_text(text: str) -> str:
    """对一段文本脱敏：手机号、api key、Bearer、wechat 类 token。"""
    if not isinstance(text, str):
        return str(text)

    def _phone(m: "re.Match[str]") -> str:
        d = m.group(1)
        return f"{d[:3]}****{d[7:]}"

    text = _PHONE_RE.sub(_phone, text)
    # 先处理键值对与 Bearer（保留键名，掩码值）
    text = _KEYVALUE_RE.sub(lambda m: f"{m.group(1)}={_mask_token(m.group(2))}", text)
    text = _BEARER_RE.sub(lambda m: f"Bearer {_mask_token(m.group(1))}", text)
    # 游离的 sk- token 独立掩码（未被上面的键值对消费时）
    text = _SK_TOKEN_RE.sub(lambda m: _mask_token(m.group(1)), text)
    # wechat 值全掩（不保留前缀）
    text = _WECHAT_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return text


def mask_value(value: Any) -> Any:
    """递归脱敏一个结构化值（str / dict / list）。"""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            kd = str(k).lower()
            if kd in ("ai_api_key", "api_key", "api-secret", "secret", "password"):
                out[k] = "***" if isinstance(v, str) and v else v
            else:
                out[k] = mask_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return [mask_value(x) for x in value]
    return value


class JsonFormatter(logging.Formatter):
    """把日志记录格式化为 JSON；自动脱敏 message 与 extra 中的敏感值。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_text(record.getMessage()),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        for f in _STRUCT_FIELDS:
            v = getattr(record, f, None)
            if v is not None:
                payload[f] = v
        # 其余 extra 属性（排除 LogRecord 标准实例属性）并入，作为结构化上下文
        for k, v in record.__dict__.items():
            if k in _STRUCT_FIELDS or k in _STD_RECORD_ATTRS:
                continue
            try:
                json.dumps(v)
            except (TypeError, ValueError):
                continue
            payload[k] = mask_value(v)
        return json.dumps(payload, ensure_ascii=False)


class SensitiveDataFilter(logging.Filter):
    """logging.Filter 包装：脱敏 record.msg 与 record.args（供非 JSON handler 使用）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = mask_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def build_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """构造一个使用 JSON formatter 的独立 logger（不含全局 handler，避免污染 root）。

    调用方自行决定 handler 挂载；`logger.handlers = []` 时记录落在 root 的 LastResort
    以 WARNING 兜底，生产由应用入口统一 `logging.basicConfig` 装配。
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger
