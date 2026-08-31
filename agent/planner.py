"""agent/planner.py — 真 LLM 决策器（SDD Step 6.2，补 §4.1 planner 接缝欠账）。

把 graph 的 planner 接缝 `planner(messages, tool_schemas) -> decision` 接到 Step 2.2
`interview/llm_client.llm_chat_functions`（OpenAI 兼容 function-calling，DeepSeek 支持）。
decision 三态契约与 §4.1 完全一致，graph/service/registry 零结构改动：

    {"action": "tool",     "name": str, "arguments": dict}
    {"action": "ask_user", "question": str}
    {"action": "report",   "content": str}

职责边界：
- **system prompt 组装（L0 保持）**：`defense.SYSTEM_PROMPT`（5.2 服务端安全常量）原样
  前置 + `OPERATIONAL_RULES`（§4.2/§4.4 硬规则的 LLM 可读版：先查库存再投库存再搜新、
  缺必填先 ask_user 禁止编默认值、JobStatus 词汇）；
- **ask_user 伪工具**：§4.2 定义 ask_user 为工具，但 graph 把它实现为决策路由（非
  registry 执行件）——本模块在调用 LLM 前把 ask_user 的 OpenAI schema 追加进 tool_schemas
  副本（只进 LLM 声明、**不进 ToolRegistry 白名单**），LLM 调它 → ask_user 决策；
- **trace → OpenAI 规范消息映射**：graph trace 是内部消息历史（user / assistant{decision}
  / tool），OpenAI 兼容服务端要求 role:tool 消息必须配对前面的 assistant tool_calls——
  映射器给 tool 决策生成 `call_N` id，后续 role:tool 结果带上同一 tool_call_id；
- **失败降级**：LLM 调用异常（网络/HTTP/裸 key）→ ERROR 日志 + report 决策，回合诚实
  收尾不 500；
- **key 探测**：`llm_planner_factory` 无 api_key 返回 None（service 回退 echo，2.4
  「无 key 可冒烟」承诺不破）。

本模块永不触碰 Playwright/浏览器；工具执行与白名单校验仍全部在 graph 的 execute 节点
（安全边界 L3），这里只做"LLM 输出 → 合法 decision"的解析。
"""

from __future__ import annotations

import json
from typing import Any

from agent import defense
from agent.log_config import build_logger
from interview.llm_client import _load_ai_config, llm_chat_functions

__all__ = [
    "OPERATIONAL_RULES",
    "PLANNER_SYSTEM_PROMPT",
    "ASK_USER_TOOL_SCHEMA",
    "llm_planner_factory",
    "default_planner_factory",
    "parse_llm_message",
    "trace_to_openai_messages",
]

logger = build_logger("agent.planner")

PLANNER_TEMPERATURE = 0.2  # 决策稳定性（纯文本闲聊用 0.3+，工具决策取更低）

# DeepSeek 思考模式（deepseek-v4-pro 等）要求多轮回传 reasoning_content（思维链），
# 而决策循环每轮回灌的是 graph 内部 trace（不含原始 reasoning）——实测 replan 必 400。
# Agent 决策循环关闭思考模式：不再依赖 reasoning 回传，且逐工具重规划的时延/Token 大降。
DISABLE_THINKING_BODY = {"thinking": {"type": "disabled"}}


def _extra_body(cfg: dict) -> dict | None:
    """按供应商给请求体扩展：DeepSeek 端点关闭思考模式，其他端点原样（不发未知字段）。"""
    if "deepseek" in (cfg.get("base_url") or "").lower():
        return DISABLE_THINKING_BODY
    return None

# §4.2/§4.4 硬规则的 LLM 可读版：system prompt 的操作规则段（安全声明在 SYSTEM_PROMPT）。
OPERATIONAL_RULES = (
    "工作规则（必须遵守）：\n"
    "1. 打招呼/投递前，必须先用 query_jobs(ungreeted=true) 查本地岗位库存；"
    "有库存就先用 send_greetings 投库存，库存不够再考虑 search_jobs 搜新的（max_pages≤3；"
    "用户提到全职/实习/兼职时必须传 job_type 参数，不要把“实习”这类词塞进关键词里）。\n"
    "2. 岗位状态词汇（applications.status）：discovered（搜索新入库）、pending（存量待投）、"
    "greeted（已打招呼）、applied/replied/interview（已投递对话中）、filtered（被关键词过滤）、"
    "unknown（结果未知，等人工确认）。\n"
    "3. 缺必填信息（城市、数量、关键词不明）时调用 ask_user 工具反问，禁止自行假设或编默认值；"
    "反问一次仍得不到，再给带默认值的确认式问题（例如“那我按上海、10 个来执行？”）。\n"
    "3a. 投递（send_greetings）前用户没说要投哪类岗位时，先 ask_user 反问想投的关键词"
    "（例如“库存里有‘大模型’‘Agent’‘后端’等，你想投哪类？”），拿到后传给 send_greetings 的 "
    "keyword 参数（只投岗位名/公司命中的库存）；用户明确说“都投/不限”才可不传 keyword——"
    "禁止把不加筛选的整批库存全投出去。\n"
    "4. 动手前可先用 get_progress 查今日已投与剩余额度；额度用完就停下并如实告知。\n"
    "5. 汇报用中文、简洁、带数字结果（新增几条/已投几个/还剩几条）；汇报与反问直接给正文"
    "（可用 Markdown），不要把决策包成 JSON 文本输出；"
    "后台任务的进度由系统推送，不要虚构任务状态。\n"
    "6. 工具返回 <untrusted> 中的 error 字段说明参数被拒，按 allowed/提示修正后重试。\n"
    "7. 浏览器生命周期：search_jobs/send_greetings 执行前系统会自动预检浏览器——收到"
    "「浏览器未启动」的 error 时，先调 open_browser 开启浏览器，成功后原样重试原工具；"
    "用户要求释放资源/收工时可用 close_browser（有任务在跑会被系统拒绝，如实转告即可，"
    "不要反复重试）。"
)

PLANNER_SYSTEM_PROMPT = defense.SYSTEM_PROMPT + "\n\n" + OPERATIONAL_RULES

# ask_user 伪工具的 OpenAI schema（§4.2：反问机制本身实现为工具，走同一条结构化通道）。
ASK_USER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "向用户反问澄清缺失的必填信息（城市/数量/关键词等）。"
            "缺必填信息时必须先反问，禁止自行假设或编默认值。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问用户的问题（一次问清所有缺项）"}
            },
            "required": ["question"],
        },
    },
}


def llm_planner_factory(user_input: str):
    """有 AI key 返回真 LLM planner，无 key 返回 None（service 缺省链回退 echo）。

    key 探测复用 Step 0.2 链路（`_load_ai_config`：env 优先、settings 表兜底），
    探测失败（如库未初始化）视同无 key。
    """
    try:
        cfg = _load_ai_config()
    except Exception:  # noqa: BLE001 — 探测失败视同无 key，回退 echo
        return None
    if not cfg.get("api_key"):
        return None

    def _planner(messages, tool_schemas):
        llm_messages = trace_to_openai_messages(messages)
        tools = list(tool_schemas) + [ASK_USER_TOOL_SCHEMA]
        try:
            message = llm_chat_functions(
                messages=llm_messages,
                tools=tools,
                system_prompt=PLANNER_SYSTEM_PROMPT,
                temperature=PLANNER_TEMPERATURE,
                extra_body=_extra_body(cfg),
            )
        except Exception as exc:  # noqa: BLE001 — 网络/HTTP/裸 key：诚实降级收尾，回合不 500
            logger.error("LLM 决策调用失败: %s", exc)
            return {"action": "report", "content": f"LLM 调用失败：{exc}"}
        return parse_llm_message(message)

    return _planner


def default_planner_factory(user_input: str):
    """service 缺省 planner 链（Step 6.2）：有 AI key 用真 LLM，无 key 回退 echo。"""
    return llm_planner_factory(user_input) or _echo_planner(user_input)


def _echo_planner(user_input: str):
    """echo 兜底 planner（与 service.echo_planner_factory 同语义；独立定义防循环 import）。"""

    def _planner(messages, tool_schemas):
        if not any(m.get("role") == "tool" for m in messages):
            return {"action": "tool", "name": "echo", "arguments": {"text": user_input}}
        return {"action": "report", "content": f"已回显：{user_input}"}

    return _planner


def parse_llm_message(message: dict) -> dict:
    """OpenAI assistant message dict → graph decision（三态）。

    - `tool_calls` 非空 → 只取第一个（多调用 WARNING 忽略：决策图一次执行一个工具）；
      name=="ask_user" → ask_user 决策；否则 tool 决策。arguments 非法 JSON/非 dict →
      `{}`，交给工具的 Pydantic L3 校验回 error dict 自纠（§3.1 先例），这里绝不抛。
    - `tool_calls` 为空 → content 即最终答复（report）。ask_user 不靠文本解析——
      OPERATIONAL_RULES 已指示 LLM 用工具通道反问。
    """
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        if len(tool_calls) > 1:
            logger.warning("LLM 一次返回 %d 个 tool_calls，只取第一个（决策图一次执行一个工具）", len(tool_calls))
        tc = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args = _parse_arguments(fn.get("arguments"))
        if name == "ask_user":
            return {"action": "ask_user", "question": str(args.get("question", ""))}
        return {"action": "tool", "name": name, "arguments": args}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        content = "（无内容）"
    return _unwrap_text_decision(content)


def _unwrap_text_decision(content: str) -> dict:
    """content 本身是决策 JSON 文本时解包（V1.2.26 hotfix，绝不抛）。

    模型偶尔用纯文本输出决策（用户实测：气泡直接显示 {"action":"report",...} 原始 JSON）。
    - action=report / ask_user：取 content/question 正文，正常收尾或反问；
    - action=tool 等其它形状：**不执行**（执行决策只认 tool_calls 结构化通道，白名单与
      审批门都接在这条通道上），原文进 report 让回合正常结束；
    - 非 JSON / 解析失败：原文进 report（原行为）。
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return {"action": "report", "content": content}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"action": "report", "content": content}
    if not isinstance(parsed, dict):
        return {"action": "report", "content": content}
    if parsed.get("action") == "ask_user":
        return {"action": "ask_user", "question": str(parsed.get("question") or "")}
    if parsed.get("action") == "report":
        body = parsed.get("content")
        return {"action": "report", "content": str(body) if body else "（无内容）"}
    return {"action": "report", "content": content}


def _parse_arguments(raw: Any) -> dict:
    """tool_calls.function.arguments → dict；非法输入一律 {}（自纠通道，不抛）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def trace_to_openai_messages(trace: list[dict]) -> list[dict]:
    """graph trace → OpenAI 规范消息（tool_call_id 配对）。

    - `{role:user}` → user 原样（含 L0 `<user_input>` 包裹，graph 已注入）；
    - `{role:assistant, decision}` → action=tool 转 assistant.tool_calls（id=`call_N`，
      arguments=JSON 字符串）；ask_user/report 决策转 assistant **纯文本正文**（V1.2.26
      起不再回灌 decision JSON——防模型模仿用文本输出决策）；
    - `{role:tool, content}` → `{"role":"tool","tool_call_id":<最近未配对 call_N>}`——
      OpenAI 兼容服务端要求 tool 消息必须配对前面的 assistant tool_calls，裸 role:tool
      不合规；审批拒绝回灌（"用户拒绝了工具 X"）同此映射。
    """
    out: list[dict] = []
    pending_id: str | None = None
    n = 0
    for m in trace:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            dec = m.get("decision") or {}
            n += 1
            if dec.get("action") == "tool":
                call_id = f"call_{n}"
                out.append(
                    {
                        "role": "assistant",
                        "content": dec.get("content") or None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": dec.get("name", ""),
                                    "arguments": json.dumps(dec.get("arguments", {}), ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
                pending_id = call_id
            else:
                # ask_user/report 决策回灌**纯文本正文**（V1.2.26）：原实现回灌 decision JSON
                # 字符串，模型会照猫画虎也用文本输出决策 → 前端气泡显示原始 JSON。决策轨迹
                # 已落 agent_steps（transcript 可回放），LLM 上下文给正文即可；异常形状兜底
                # 仍回 JSON 保持可自省。
                body = dec.get("content") or dec.get("question")
                text = body if isinstance(body, str) and body else json.dumps(dec, ensure_ascii=False)
                out.append({"role": "assistant", "content": text})
                pending_id = None
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": pending_id or f"call_orphan_{n}",
                    "content": m.get("content", ""),
                }
            )
            pending_id = None
    return out
