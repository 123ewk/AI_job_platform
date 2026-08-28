"""Step 0.2 密钥外移 — _load_ai_config 读取优先级测试。

规则（SDD §5 密钥管理）：
- AI_API_KEY 环境变量 / .env 文件优先
- settings 表旧值兜底（迁移期兼容）
- 两者皆无 → 空 key（由调用方走"未配置"分支）
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boss_state
import interview.llm_client as llm_client


def setup_module(module):
    # 与 test_boss_state.py 相同模式：测试用临时库，避免污染 .boss_profile
    boss_state._local.conn = None
    tmp = Path(tempfile.gettempdir()) / "boss_state_llm_cfg_test.db"
    if tmp.exists():
        tmp.unlink()
    boss_state.DB_PATH = tmp
    boss_state.init_db()


def _isolate_env(monkeypatch):
    """隔离环境：environ 换成副本（load_dotenv 的写入不外泄），指向不存在的 .env。"""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("AI_API_KEY", None)
    monkeypatch.setattr(llm_client, "_DOTENV_PATH", str(Path(tempfile.gettempdir()) / "no_such_dotenv.env"))


def test_settings_fallback_when_no_env(monkeypatch):
    """无 env、settings 有旧值 → 用旧值（迁移期兼容）"""
    _isolate_env(monkeypatch)
    boss_state.set_setting("ai_api_key", "sk-from-db")
    cfg = llm_client._load_ai_config()
    assert cfg["api_key"] == "sk-from-db"


def test_env_overrides_settings(monkeypatch):
    """两者都有 → env 优先"""
    _isolate_env(monkeypatch)
    boss_state.set_setting("ai_api_key", "sk-from-db")
    monkeypatch.setenv("AI_API_KEY", " sk-from-env ")
    cfg = llm_client._load_ai_config()
    assert cfg["api_key"] == "sk-from-env"


def test_env_only(monkeypatch):
    """只有 env → 用 env 值"""
    _isolate_env(monkeypatch)
    monkeypatch.setenv("AI_API_KEY", "sk-from-env")
    cfg = llm_client._load_ai_config()
    assert cfg["api_key"] == "sk-from-env"


def test_empty_when_nothing_configured(monkeypatch):
    """两者皆无 → 空 key"""
    _isolate_env(monkeypatch)
    boss_state.set_setting("ai_api_key", "")
    cfg = llm_client._load_ai_config()
    assert cfg["api_key"] == ""


def test_dotenv_file_is_loaded(monkeypatch, tmp_path):
    """.env 文件中的 AI_API_KEY 被读取，且优先于 settings 旧值"""
    env_file = tmp_path / ".env"
    env_file.write_text("AI_API_KEY=sk-from-dotenv-file\n", encoding="utf-8")
    boss_state.set_setting("ai_api_key", "sk-from-db")
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("AI_API_KEY", None)
    monkeypatch.setattr(llm_client, "_DOTENV_PATH", str(env_file))
    cfg = llm_client._load_ai_config()
    assert cfg["api_key"] == "sk-from-dotenv-file"


def test_base_url_and_model_still_from_settings(monkeypatch):
    """非密钥配置（base_url/model）行为不变：仍从 settings 读取"""
    _isolate_env(monkeypatch)
    boss_state.set_setting("ai_base_url", "https://example.com/v1")
    boss_state.set_setting("ai_model", "my-model")
    cfg = llm_client._load_ai_config()
    assert cfg["base_url"] == "https://example.com/v1"
    assert cfg["model"] == "my-model"
