"""
面试问答Agent - LLM客户端模块
- Embedding: Ollama nomic-embed-text
- 出题: Ollama qwen2.5:14b
- 批改: DeepSeek API
"""

import json
import os
import re
from typing import List, Optional

import httpx
import numpy as np

# Ollama配置
OLLAMA_BASE = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL = "qwen2.5:14b"


# Step 0.2 密钥外移：AI_API_KEY 从 .env/环境变量读取，settings 表旧值兜底（迁移期兼容）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


def _load_ai_config():
    cfg = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    }
    # 1) .env / 环境变量优先（python-dotenv 缺失时退化为只读进程环境变量）
    try:
        from dotenv import load_dotenv

        load_dotenv(_DOTENV_PATH)
    except Exception:
        pass
    env_key = os.environ.get("AI_API_KEY", "").strip()
    if env_key:
        cfg["api_key"] = env_key
    # 2) settings 表兜底：存量用户的 key 还在库里；base_url/model 行为不变
    try:
        import sys

        sys.path.insert(0, _PROJECT_ROOT)
        from boss_state import get_db, get_setting

        get_db()
        if not env_key:
            key = get_setting("ai_api_key")
            if key:
                cfg["api_key"] = key
        url = get_setting("ai_base_url")
        if url:
            cfg["base_url"] = url
        model = get_setting("ai_model")
        if model:
            cfg["model"] = model
    except Exception:
        pass
    return cfg


def get_embedding(text: str) -> List[float]:
    """获取文本的embedding向量"""
    resp = httpx.post(
        f"{OLLAMA_BASE}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["embeddings"][0]


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """计算余弦相似度"""
    a = np.array(vec_a, dtype=np.float32)
    b = np.array(vec_b, dtype=np.float32)
    if np.linalg.norm(a) == 0 or np.linalg.norm(b) == 0:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def llm_chat_ollama(messages: list, system_prompt: Optional[str] = None, temperature: float = 0.7) -> str:
    """调用Ollama大模型（出题用）"""
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    resp = httpx.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"]


def llm_chat_deepseek(messages: list, system_prompt: Optional[str] = None, temperature: float = 0.3) -> str:
    """调用AI API（懒加载配置，每次从SQLite读取）"""
    cfg = _load_ai_config()
    if not cfg["api_key"]:
        raise RuntimeError("AI API Key未配置，请在设置页配置")

    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }

    resp = httpx.post(
        f"{cfg['base_url']}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_json_from_llm(text: str) -> Optional[dict]:
    """从LLM返回文本中提取JSON"""
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return None
