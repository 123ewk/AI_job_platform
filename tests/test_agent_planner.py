"""SDD Step 6.2：真 LLM planner 接入（红→绿，先红）。

补 §4.1「plan 接缝 Phase 3 接 llm_chat_functions」的欠账：Phase 3/4 只接了工具，
service 缺省 planner 仍是 echo 假 planner，自然语言不被理解（DoD #1 不成立）。本文件
验收 `agent/planner.py` 把接缝接到 Step 2.2 `llm_chat_functions`：

1. **decision 解析**：tool_calls→tool 决策（arguments JSON 解析）、空 tool_calls→report、
   ask_user 伪工具→ask_user 决策、多 tool_calls 只取第一个、arguments 非法 JSON→{}；
2. **失败降级**：llm_chat_functions 抛异常 → report 决策（诚实收尾，回合不 500）；
3. **key 探测**：无 key → `llm_planner_factory` 返回 None（service 回退 echo）；
4. **trace→OpenAI 消息映射**：assistant tool_calls 与 role:tool 结果按 tool_call_id
   配对（OpenAI 兼容服务端要求，裸 role:tool 不合规；审批拒绝回灌同此映射）；
5. **system prompt 组装**：defense.SYSTEM_PROMPT（L0 服务端常量）+ OPERATIONAL_RULES
   （先查库存/ask_user 反问/JobStatus 词汇）；ask_user 伪工具只进 LLM 声明不进白名单；
6. **service 缺省链**：无 key 用 echo（2.4「无 key 可冒烟」不破）/ 有 key（mock）用真
   LLM 端到端；
7. **graph 未注册工具 error 回灌自纠 E2E**：真 LLM 幻觉工具名不再 KeyError 炸回合，
   error dict（含 allowed 白名单）回灌 LLM 自纠（§3.1 先例）。

mock/隔离策略：monkeypatch `agent.planner._load_ai_config`（key 探测）与
`agent.planner.llm_chat_functions`（LLM 调用），不打真 HTTP；graph E2E 用内存 SQLite
（StaticPool，跨线程共享连接）+ 脚本化假 planner，不碰浏览器。
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from agent import defense, graph, state
from agent import planner as planner_mod
from agent.graph import ToolRegistry
from agent.service import AgentService
from db import models

# ──────────────────────────────────────────────────────────
#  夹具
# ──────────────────────────────────────────────────────────


def _engine():
    # StaticPool + check_same_thread=False：AgentService.chat 用 asyncio.to_thread 在
    # 工作线程 invoke 图，内存库须所有线程共享同一连接（否则 :memory: 各连接独立丢表）。
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.Base.metadata.create_all(eng)
    return eng


def _registry_with_echo() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        "echo",
        func=lambda text: {"echo": text, "received": True},
        description="回显工具（假工具，回归护栏）",
        schema={
            "type": "function",
            "function": {
                "name": "echo",
                "description": "回显工具（假工具）",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        },
        write=False,
    )
    return reg


def _llm_msg(tool_calls=None, content=None) -> dict:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _tool_call(name: str, arguments: str, call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


# ──────────────────────────────────────────────────────────
#  1. decision 解析（三态契约不变，§4.1）
# ──────────────────────────────────────────────────────────


def test_parse_tool_call_with_json_arguments():
    dec = planner_mod.parse_llm_message(_llm_msg([_tool_call("query_jobs", '{"status": "pending", "limit": 5}')]))
    assert dec == {"action": "tool", "name": "query_jobs", "arguments": {"status": "pending", "limit": 5}}


def test_parse_plain_content_is_report():
    dec = planner_mod.parse_llm_message(_llm_msg(content="今日已投 3 个"))
    assert dec == {"action": "report", "content": "今日已投 3 个"}


def test_parse_empty_content_is_report_placeholder():
    dec = planner_mod.parse_llm_message(_llm_msg())
    assert dec["action"] == "report"
    assert dec["content"]  # 非空占位，report 节点不落空汇报


def test_parse_ask_user_pseudo_tool():
    dec = planner_mod.parse_llm_message(_llm_msg([_tool_call("ask_user", '{"question": "要投几个岗位？"}')]))
    assert dec == {"action": "ask_user", "question": "要投几个岗位？"}


def test_parse_takes_first_tool_call_only():
    msg = _llm_msg([_tool_call("query_jobs", "{}", "a"), _tool_call("echo", "{}", "b")])
    dec = planner_mod.parse_llm_message(msg)
    assert dec["action"] == "tool" and dec["name"] == "query_jobs"


def test_parse_invalid_arguments_json_becomes_empty_dict():
    # 非法 JSON → {} 交给工具 Pydantic L3 校验回 error dict 自纠，不抛
    dec = planner_mod.parse_llm_message(_llm_msg([_tool_call("query_jobs", "not json")]))
    assert dec == {"action": "tool", "name": "query_jobs", "arguments": {}}


def test_parse_non_dict_arguments_becomes_empty_dict():
    dec = planner_mod.parse_llm_message(_llm_msg([_tool_call("query_jobs", "[1, 2]")]))
    assert dec["arguments"] == {}


# ──────────────────────────────────────────────────────────
#  2+3. 失败降级 + key 探测
# ──────────────────────────────────────────────────────────


def test_planner_degrades_to_report_on_llm_error(monkeypatch):
    monkeypatch.setattr(planner_mod, "_load_ai_config", lambda: {"api_key": "sk-test", "base_url": "u", "model": "m"})

    def _boom(**_kwargs):
        raise RuntimeError("网络超时")

    monkeypatch.setattr(planner_mod, "llm_chat_functions", _boom)
    p = planner_mod.llm_planner_factory("你好")
    assert p is not None
    dec = p(messages=[{"role": "user", "content": "<user_input>你好</user_input>"}], tool_schemas=[])
    assert dec["action"] == "report"
    assert "LLM 调用失败" in dec["content"]


def test_factory_returns_none_without_key(monkeypatch):
    monkeypatch.setattr(planner_mod, "_load_ai_config", lambda: {"api_key": "", "base_url": "u", "model": "m"})
    assert planner_mod.llm_planner_factory("你好") is None


def test_factory_planner_passes_system_prompt_and_ask_user_schema(monkeypatch):
    monkeypatch.setattr(planner_mod, "_load_ai_config", lambda: {"api_key": "sk-test", "base_url": "u", "model": "m"})
    captured: dict = {}

    def _fake_llm(messages, tools, system_prompt=None, temperature=0.3, tool_choice="auto", extra_body=None):
        captured.update(
            messages=messages, tools=tools, system_prompt=system_prompt, temperature=temperature, extra_body=extra_body
        )
        return _llm_msg(content="好")

    monkeypatch.setattr(planner_mod, "llm_chat_functions", _fake_llm)
    schemas = [{"type": "function", "function": {"name": "query_jobs", "description": "", "parameters": {}}}]
    p = planner_mod.llm_planner_factory("你好")
    p(messages=[{"role": "user", "content": "u"}], tool_schemas=schemas)
    assert defense.SYSTEM_PROMPT in captured["system_prompt"]  # L0 服务端常量前置
    tool_names = [t["function"]["name"] for t in captured["tools"]]
    assert tool_names == ["query_jobs", "ask_user"]  # ask_user 伪工具只进 LLM 声明（不进 registry）
    assert captured["temperature"] == 0.2  # 决策稳定性
    assert captured["extra_body"] is None  # 非 DeepSeek 端点不发供应商扩展字段


def test_extra_body_disables_thinking_for_deepseek_only(monkeypatch):
    # DeepSeek 思考模式要求多轮回传 reasoning_content（决策循环回灌的是内部 trace，没有
    # reasoning）→ replan 必 400；对 DeepSeek 端点关闭思考，其他端点不发未知字段。
    assert planner_mod._extra_body({"base_url": "https://api.deepseek.com/v1"}) == planner_mod.DISABLE_THINKING_BODY
    assert planner_mod._extra_body({"base_url": "https://api.openai.com/v1"}) is None
    assert planner_mod._extra_body({"base_url": None}) is None
    assert planner_mod._extra_body({}) is None


# ──────────────────────────────────────────────────────────
#  4. trace → OpenAI 规范消息映射（tool_call_id 配对）
# ──────────────────────────────────────────────────────────


def test_trace_mapping_pairs_tool_call_ids():
    trace = [
        {"role": "user", "content": "<user_input>帮我查</user_input>"},
        {"role": "assistant", "decision": {"action": "tool", "name": "query_jobs", "arguments": {"status": "pending"}}},
        {"role": "tool", "content": '<untrusted>{"tool": "query_jobs", "output": {}}</untrusted>'},
        {"role": "assistant", "decision": {"action": "report", "content": "查完了"}},
    ]
    out = planner_mod.trace_to_openai_messages(trace)
    assert out[0] == {"role": "user", "content": "<user_input>帮我查</user_input>"}
    call = out[1]["tool_calls"][0]
    assert call["id"] and out[2]["tool_call_id"] == call["id"]  # 工具结果与调用配对
    assert call["function"]["name"] == "query_jobs"
    assert json.loads(call["function"]["arguments"]) == {"status": "pending"}
    assert out[2]["role"] == "tool" and "untrusted" in out[2]["content"]  # L1 包裹原样透传
    assert out[3]["role"] == "assistant"  # report 决策转 assistant 文本
    assert json.loads(out[3]["content"])["action"] == "report"


def test_trace_mapping_reject_feedback_pairs_with_tool_decision():
    # 审批拒绝回灌（"用户拒绝了工具 X"）也按 role:tool 配对最近的 assistant tool_calls
    trace = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "decision": {"action": "tool", "name": "send_greetings", "arguments": {}}},
        {"role": "tool", "content": "用户拒绝了工具 send_greetings（reject）。请另选方案或收尾。"},
    ]
    out = planner_mod.trace_to_openai_messages(trace)
    assert out[2]["tool_call_id"] == out[1]["tool_calls"][0]["id"]


# ──────────────────────────────────────────────────────────
#  5. system prompt 组装（L0 保持 + §4.2/§4.4 硬规则）
# ──────────────────────────────────────────────────────────


def test_system_prompt_composes_defense_and_operational_rules():
    assert defense.SYSTEM_PROMPT in planner_mod.PLANNER_SYSTEM_PROMPT
    for marker in ("query_jobs", "ungreeted", "ask_user", "禁止", "discovered"):
        assert marker in planner_mod.PLANNER_SYSTEM_PROMPT


# ──────────────────────────────────────────────────────────
#  6. service 缺省链：无 key 回退 echo / 有 key 用真 LLM
# ──────────────────────────────────────────────────────────


def test_default_chain_falls_back_to_echo_without_key(monkeypatch):
    monkeypatch.setattr(planner_mod, "_load_ai_config", lambda: {"api_key": "", "base_url": "u", "model": "m"})

    def _fail_llm(**_kwargs):  # 无 key 时绝不应触达 LLM
        raise AssertionError("无 key 不应调用 LLM")

    monkeypatch.setattr(planner_mod, "llm_chat_functions", _fail_llm)
    svc = AgentService(engine=_engine(), registry=_registry_with_echo())  # 不注入 make_planner

    async def _chat():
        return await svc.chat("hi", "t-echo")

    out = asyncio.run(_chat())
    assert out["status"] == "completed"
    assert out["report"].startswith("已回显")  # echo 兜底（2.4 冒烟承诺不破）


def test_default_chain_uses_real_llm_when_key_present(monkeypatch):
    monkeypatch.setattr(planner_mod, "_load_ai_config", lambda: {"api_key": "sk-test", "base_url": "u", "model": "m"})
    responses = iter(
        [
            _llm_msg([_tool_call("echo", '{"text": "自然语言"}')]),
            _llm_msg(content="查完了：库存 2 条"),
        ]
    )
    monkeypatch.setattr(planner_mod, "llm_chat_functions", lambda **_kw: next(responses))
    svc = AgentService(engine=_engine(), registry=_registry_with_echo())  # 不注入 make_planner

    async def _chat():
        return await svc.chat("自然语言", "t-llm")

    out = asyncio.run(_chat())
    assert out["status"] == "completed"
    assert out["report"] == "查完了：库存 2 条"  # 真 LLM 决策驱动了 echo 执行 + 汇报


# ──────────────────────────────────────────────────────────
#  7. graph：未注册工具 error 回灌自纠（KeyError → error dict，§3.1 先例）
# ──────────────────────────────────────────────────────────


def test_graph_unregistered_tool_feeds_error_back_and_recovers():
    eng = _engine()
    reg = _registry_with_echo()
    decisions = iter(
        [
            {"action": "tool", "name": "bad_tool_name", "arguments": {}},  # 真 LLM 幻觉工具名
            {"action": "report", "content": "该工具不存在，已按现有信息收尾"},
        ]
    )

    def planner(messages, tool_schemas):
        return next(decisions)

    compiled = graph.build_agent_graph(planner=planner, registry=reg, engine=eng)
    out = compiled.invoke(
        {"thread_id": "t-bad", "user_input": "hi", "execution_mode": "audit"},
        {"thread_id": "t-bad", "recursion_limit": graph.DEFAULT_RECURSION_LIMIT},
    )
    assert out["report"] == "该工具不存在，已按现有信息收尾"  # 回合不炸（原实现 KeyError 500）
    tool_msgs = [m for m in out["trace"] if m["role"] == "tool"]
    assert any("未注册的工具" in m["content"] and "bad_tool_name" in m["content"] for m in tool_msgs)
    # transcript 落了 execute 步骤且 tool_output 带 error + allowed 白名单（LLM 自纠依据）
    with SASession(eng) as s:
        rows = (
            s.execute(select(models.AgentStep).where(models.AgentStep.kind == state.StepKind.EXECUTE))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert "error" in rows[0].tool_output
        assert rows[0].tool_output["allowed"] == ["echo"]
