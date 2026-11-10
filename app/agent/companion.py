"""主控 Agent（Day6.3）：ReAct + Tool Calling 循环。

边界：
- 只有多意图/需要组合动作的句子才进入这里（``looks_multi_intent``），
  普通闲聊继续走原有流式 LLM，避免无谓的工具轮次；
- 最多 4 步、每步最多 2 个工具，并复用 Day5.4 的 ``ToolLoopGuard``；
- 工具参数里的 ``elder_id`` 一律忽略，执行上下文由服务端注入；
- 任何失败都返回可朗读的降级结果，不向长者抛异常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools import ToolContext, ToolRegistry, build_default_registry
from app.integrations.llm import LlmUnavailable, complete_chat_message
from app.services.tool_guard import ToolLoopGuard, ToolLoopTripped

AGENT_MAX_STEPS = 4
TOOLS_PER_STEP = 2

_AGENT_GUIDE = (
    "你可以调用工具来完成事情。规则：\n"
    "1. 需要天气、时间、用药、媒体播放、通知家属时，先调用对应工具，不要凭记忆编造；\n"
    "2. 一句话里有多个需求时，按用户说到的顺序依次调用，每个需求都要照顾到；\n"
    "3. 通知家属属于高危动作，工具只会转人工确认，你必须如实告诉用户「已转人工确认」；\n"
    "4. 工具失败就直说暂时不可用，不要编造结果；\n"
    "5. 最终回复要短、口语化，适合老人听，不要提工具名或技术细节。"
)

# 多意图信号：命中两类及以上，或用「然后/顺便」等顺承词串起一个动作。
# 媒体点播与播放控制分开计数：一句话里同时出现两者也必须进主控 Agent。
_SIGNALS: dict[str, tuple[str, ...]] = {
    "medication": ("吃药", "服药", "药盒", "药片", "降压药", "漏吃"),
    "media_play": (
        "放戏",
        "听戏",
        "越剧",
        "京剧",
        "黄梅戏",
        "沪剧",
        "老歌",
        "电影",
        "评弹",
        "戏曲",
        "邓丽君",
    ),
    "media_control": (
        "大声点",
        "小声点",
        "声音大",
        "声音小",
        "音量大",
        "音量小",
        "调大",
        "调小",
        "换一段",
        "换台",
        "换一首",
        "下一段",
        "下一首",
        "别放",
        "停下",
    ),
    "notify": ("告诉", "通知", "联系", "打电话", "发消息", "转告"),
    "weather": ("天气", "下雨", "下雪", "降温", "冷不冷", "热不热"),
    "time": ("几点", "什么时间", "几号", "星期几", "时间了"),
    "health": ("血压", "血糖", "心率", "血氧"),
}

# 顺承词：只有一类信号时，出现这些词说明后面还跟着另一个需求。
# 不收单字「再」和「还有」：老年人常说「再听一遍」「降压药还有几颗」，
# 这类单一意图被误升级会多一次模型轮次。
_CONNECTORS = ("然后", "顺便", "接着", "另外", "同时", "以及")


@dataclass
class AgentResult:
    text: str
    tool_names: list[str] = field(default_factory=list)
    client_events: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    degraded: str | None = None


class AgentUnavailable(RuntimeError):
    """Agent 无法给出最终回复：上层回落到流式 LLM 或模板。"""


def intent_signal_count(text: str) -> int:
    """命中多少个互不相同的需求类别（媒体点播/播放控制分别计数）。"""
    return sum(1 for words in _SIGNALS.values() if any(word in text for word in words))


def looks_multi_intent(text: str) -> bool:
    """粗筛多意图：命中两类以上工具信号，或顺承词串起一个动作。"""
    hits = intent_signal_count(text)
    if hits >= 2:
        return True
    return hits >= 1 and any(word in text for word in _CONNECTORS)


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def respond(
    text: str,
    *,
    system_prompt: str,
    ctx: ToolContext,
    history: list[Any] | None = None,
    context_text: str | None = None,
    registry: ToolRegistry | None = None,
    guard: ToolLoopGuard | None = None,
) -> AgentResult:
    """跑完 ReAct 循环并返回最终回复、工具轨迹与前端事件。"""
    tools = registry or build_default_registry()
    loop_guard = guard or ToolLoopGuard()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system_prompt}\n\n{_AGENT_GUIDE}"}
    ]
    if context_text:
        messages.append({"role": "system", "content": context_text})
    for item in history or []:
        messages.append(
            {
                "role": getattr(item, "role", "user"),
                "content": getattr(item, "content", ""),
            }
        )
    messages.append({"role": "user", "content": text})

    tool_names: list[str] = []
    client_events: list[dict[str, Any]] = []

    for step in range(1, AGENT_MAX_STEPS + 1):
        try:
            message = await complete_chat_message(messages, tools=tools.schemas())
        except LlmUnavailable as exc:
            raise AgentUnavailable(str(exc)) from exc

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = str(message.get("content") or "").strip()
            if not content:
                raise AgentUnavailable("Agent 空回复")
            return AgentResult(content, tool_names, client_events, step)

        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )

        for index, call in enumerate(tool_calls):
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            if index >= TOOLS_PER_STEP:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": "本轮工具数量超出上限，已跳过。",
                    }
                )
                continue
            try:
                loop_guard.reserve(name)
            except ToolLoopTripped as exc:
                return AgentResult(
                    "我先帮您记下，稍后再帮您处理。",
                    tool_names,
                    client_events,
                    step,
                    degraded=f"tool_loop_guard:{exc.reason}",
                )
            result = await tools.call(name, _parse_args(function.get("arguments")), ctx)
            tool_names.append(name)
            client_events.extend(result.client_events)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result.content,
                }
            )

    raise AgentUnavailable("Agent 步数超限")
