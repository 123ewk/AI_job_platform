"""SDD Step 2.2：llm_client function-calling 扩展验收（红→绿，先红）。

本文件先存在（红，`interview/llm_client.llm_chat_functions` / `build_tool_schema`
尚未实现），实现后绿。覆盖线索：

1. **tools 载荷正确**：request payload 含 `tools`（OpenAI 兼容传法）、`tool_choice=auto`
   默认、model 与认证头来自配置；旧函数行为不受影响。
2. **返回结构可消费**：返回 assistant message dict（content + tool_calls），
   后续决策图（Step 2.3）据此解析走 ToolRegistry。
3. **Pydantic → tools schema**：`build_tool_schema` 把 Pydantic model 转成
   OpenAI function 的 JSON-schema parameters（§4.2：工具 schema 用 Pydantic 定义）；
   无 model 时给空 properties。
4. **裸 key 报错**：未配置 key → RuntimeError（与存量 llm_chat_deepseek 一致）。

mock 策略：monkeypatch `llm_client.httpx.post` 捕获 kwargs 并回灌 canned 响应；
`llm_client._load_ai_config` 固定为测试配置，把底层 DB/env 读数完全隔离。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from interview import llm_client


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _fix_config(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_client,
        "_load_ai_config",
        lambda: {
            "api_key": "sk-test",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
        },
    )


# ──────────────────────────────────────────────────────────
#  验收 1：tools 载荷 + 认证头正确（mock httpx.post）
# ──────────────────────────────────────────────────────────


def test_chat_functions_sends_openai_compatible_payload(monkeypatch):
    _fix_config(monkeypatch)
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": "done"}}]})

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)

    tool = {
        "type": "function",
        "function": {
            "name": "query_jobs",
            "description": "查岗位",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    out = llm_client.llm_chat_functions(
        [{"role": "user", "content": "投了多少"}], tools=[tool]
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    body = captured["json"]
    assert body["model"] == "deepseek-chat"
    assert body["stream"] is False
    assert body["tools"] == [tool]
    assert body["tool_choice"] == "auto"
    assert out["content"] == "done"


def test_chat_functions_returns_tool_calls(monkeypatch):
    _fix_config(monkeypatch)
    canned = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "query_jobs",
                                "arguments": '{"status": "ungreeted"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    monkeypatch.setattr(
        llm_client.httpx, "post", lambda *a, **k: _FakeResp(canned)
    )

    out = llm_client.llm_chat_functions([{"role": "user", "content": "先查库存"}], tools=[])
    calls = out["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "query_jobs"


def test_chat_functions_tool_choice_override(monkeypatch):
    _fix_config(monkeypatch)
    body: dict = {}

    def fake_post(url, **kwargs):
        body.update(kwargs["json"])
        return _FakeResp({"choices": [{"message": {"role": "assistant"}}]})

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    llm_client.llm_chat_functions([], tools=[], tool_choice="none")
    assert body["tool_choice"] == "none"


# ──────────────────────────────────────────────────────────
#  验收 3：Pydantic → OpenAI tools schema
# ──────────────────────────────────────────────────────────


def test_build_tool_schema_from_pydantic():
    class QueryJobsParams(BaseModel):
        status: str = Field(description="按状态过滤")
        top_n: int = Field(default=5, description="取前 N 条")

    schema = llm_client.build_tool_schema("query_jobs", "查岗位库中的岗位", QueryJobsParams)

    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "query_jobs"
    assert fn["description"] == "查岗位库中的岗位"
    params = fn["parameters"]
    assert params["type"] == "object"
    # 字段描述从 Pydantic Field.description 带出（工具 schema 由模型定义，§4.2）
    param_s = _json_str(params)
    assert '"status"' in param_s and "按状态过滤" in param_s
    assert "top_n" in param_s


def _json_str(params: dict) -> str:
    import json

    return json.dumps(params, ensure_ascii=False)


def test_build_tool_schema_no_params_model():
    schema = llm_client.build_tool_schema("get_progress", "查进度")
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}


# ──────────────────────────────────────────────────────────
#  验收 4：裸 key 报错（与存量 llm_chat_deepseek 一致）
# ──────────────────────────────────────────────────────────


def test_chat_functions_raises_without_api_key(monkeypatch):
    monkeypatch.setattr(llm_client, "_load_ai_config", lambda: {"api_key": ""})
    try:
        llm_client.llm_chat_functions([], tools=[])
        raise AssertionError("应因未配置 key 抛 RuntimeError")
    except RuntimeError:
        pass


# ──────────────────────────────────────────────────────────
#  验收 2 补充：system_prompt 前置（OpenAI messages 规范）
# ──────────────────────────────────────────────────────────


def test_chat_functions_prepends_system_prompt(monkeypatch):
    _fix_config(monkeypatch)
    msgs: dict = {}

    def fake_post(url, **kwargs):
        msgs.update(kwargs["json"])
        return _FakeResp({"choices": [{"message": {"role": "assistant"}}]})

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    user_msg = [{"role": "user", "content": "hi"}]
    llm_client.llm_chat_functions(user_msg, tools=[], system_prompt="你是助手")
    assert msgs["messages"][0] == {"role": "system", "content": "你是助手"}
    assert msgs["messages"][1] == user_msg[0]
