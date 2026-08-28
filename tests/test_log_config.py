"""SDD Step 0.3：agent/log_config.py 结构化 JSON 日志 + 脱敏单测。

覆盖
- mask_text：手机号 / api_key / sk- token / wechat 掩码
- mask_value：dict/list 递归、敏感键名全掩
- JsonFormatter：输出合法 JSON、携带 ts/level/logger/message、合并 extra
  结构化字段（session_id/task_id/tool）、exc 正确序列化
- SensitiveDataFilter：对非 JSON handler 也脱敏
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.log_config import (  # noqa: E402
    JsonFormatter,
    SensitiveDataFilter,
    mask_text,
    mask_value,
)


def _fmt_record(**extra) -> str:
    logger = logging.getLogger("test.log_config")
    r = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        10,
        "申请 %s",
        ("AI算法工程师",),
        None,
        func=__name__,
        extra=extra or None,
    )
    return JsonFormatter().format(r)


# ── mask_text ─────────────────────────────────────────────


def test_mask_phone_cn():
    assert mask_text("联系电话 13812345678 提交") == "联系电话 138****5678 提交"


def test_mask_api_key_env_style():
    out = mask_text("AI_API_KEY: sk-abc123xyz456token789")
    # 明文 token 绝不外泄；sk- token 掩成 sk-a*****789（首4尾3）
    assert "sk-abc123xyz456token789" not in out
    assert "sk-a*****789" in out


def test_mask_bearer_token():
    out = mask_text("Authorization: Bearer gl-ABCdefGHIJ1234567xyz")
    assert "gl-ABCdefGHIJ1234567xyz" not in out
    assert "gl-A*****xyz" in out


def test_mask_wechat_id_value():
    out = mask_text("wechat_id: user_abc12345")
    assert "user_abc12345" not in out  # 值被截断替换
    assert "***" in out


def test_mask_normal_text_unchanged():
    s = "今天投递了3个岗位，工程师小王回复了"
    assert mask_text(s) == s


# ── mask_value ────────────────────────────────────────────


def test_mask_value_recursive():
    data = {
        "company": "字节跳动",
        "hr": {"wechat_id": "vx_hr_01", "phone": "13911112222"},
        "jobs": ["a", "b"],
    }
    out = mask_value(data)
    assert out["company"] == "字节跳动"
    assert out["hr"]["phone"] == "139****2222"
    assert out["jobs"] == ["a", "b"]


def test_mask_value_sensitive_key_fully_masked():
    out = mask_value({"ai_api_key": "sk-super-secret-value", "name": "x"})
    assert out["ai_api_key"] == "***"
    assert out["name"] == "x"


# ── JsonFormatter ─────────────────────────────────────────


def test_formatter_valid_json_and_fields():
    line = _fmt_record(session_id="sess_1", tool="apply")
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test.log_config"
    assert obj["message"] == "申请 AI算法工程师"
    assert obj["session_id"] == "sess_1"
    assert obj["tool"] == "apply"


def test_formatter_masks_phone_in_message():
    logger = logging.getLogger("test.log_config")
    r = logger.makeRecord(logger.name, logging.INFO, __file__, 10, "联系 HR 13911112222", (), None, func=__name__)
    line = JsonFormatter().format(r)
    assert "13911112222" not in line
    assert "139****2222" in line


def test_formatter_does_not_leak_std_record_attrs():
    obj = json.loads(_fmt_record())
    for bad in ("created", "thread", "levelno", "process", "threadName", "funcName"):
        assert bad not in obj


def test_formatter_includes_task_id():
    obj = json.loads(_fmt_record(task_id="task_9"))
    assert obj["task_id"] == "task_9"


def test_formatter_handles_exception_info():
    try:
        raise ValueError("boom")
    except ValueError:
        import traceback

        logger = logging.getLogger("test.log_config")
        r = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            10,
            "错误",
            (),
            sys.exc_info(),
            func=__name__,
        )
        formatter = JsonFormatter()
        out = json.loads(formatter.format(r))
        assert out["level"] == "ERROR"
        assert "ValueError" in out["exc"]
        assert traceback is not None  # 引用避免未使用


# ── SensitiveDataFilter ───────────────────────────────────


def test_filter_masks_msg_and_clears_args():
    logger = logging.getLogger("test.filter")
    r = logger.makeRecord(logger.name, logging.INFO, __file__, 10, "手机 13911112222", (), None, func=__name__)
    assert SensitiveDataFilter().filter(r) is True
    assert "139****2222" in r.getMessage()
    assert r.args == ()
