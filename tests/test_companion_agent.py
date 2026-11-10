"""Day6.3 主控 Agent 测试：ReAct 循环、工具顺序、预算熔断、多意图粗筛。"""

from __future__ import annotations

import json

import pytest

from app.agent.companion import AgentUnavailable, looks_multi_intent, respond
from app.agent.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from app.services.tool_guard import ToolLoopGuard


def _call(call_id: str, name: str, args: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


def _registry(order: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    def handler(name: str):
        async def run(args, ctx):  # noqa: ARG001
            order.append(name)
            return ToolResult(True, f"{name} 完成")

        return run

    for name in ("weather_get", "medication_query", "media_play", "notify_family"):
        registry.register(ToolSpec(name, name, {"type": "object"}, handler(name)))
    return registry


@pytest.mark.parametrize(
    "text",
    [
        "吃完药放戏再告诉我儿子",
        "我想听越剧，再问问天气",
        "帮我查下天气，然后通知我女儿",
        "今天天气怎么样，现在几点了？",
        "放段越剧，再把声音调小一点",
    ],
)
def test_multi_intent_is_detected(text):
    assert looks_multi_intent(text) is True


@pytest.mark.parametrize(
    "text", ["你好", "我想听越剧", "今天天气怎么样", "我要吃药", "现在几点了"]
)
def test_single_intent_is_not_agent(text):
    assert looks_multi_intent(text) is False


async def test_agent_react_loop_executes_tool_then_final(monkeypatch):
    order: list[str] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("c1", "weather_get", {"city": "上海"})],
        },
        {"role": "assistant", "content": "上海今天晴，适合出门。"},
    ]

    async def fake_complete(messages, tools=None, **kwargs):  # noqa: ARG001
        return replies.pop(0)

    monkeypatch.setattr("app.agent.companion.complete_chat_message", fake_complete)

    result = await respond(
        "吃完药放戏再告诉我儿子",
        system_prompt="你是康伴",
        ctx=ToolContext(elder_id="e1"),
        registry=_registry(order),
    )
    assert result.text == "上海今天晴，适合出门。"
    assert result.steps == 2
    assert order == ["weather_get"]


async def test_agent_calls_multiple_tools_in_order(monkeypatch):
    order: list[str] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _call("c1", "medication_query"),
                _call("c2", "media_play", {"genre": "越剧"}),
            ],
        },
        {"role": "assistant", "content": "药记下了，戏也给您放上。"},
    ]

    async def fake_complete(messages, tools=None, **kwargs):  # noqa: ARG001
        return replies.pop(0)

    monkeypatch.setattr("app.agent.companion.complete_chat_message", fake_complete)

    result = await respond(
        "吃完药放戏再告诉我儿子",
        system_prompt="你是康伴",
        ctx=ToolContext(elder_id="e1"),
        registry=_registry(order),
    )
    assert order == ["medication_query", "media_play"]
    assert result.tool_names == ["medication_query", "media_play"]


async def test_extra_tool_calls_are_skipped_not_looped(monkeypatch):
    order: list[str] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _call("c1", "weather_get"),
                _call("c2", "media_play"),
                _call("c3", "notify_family", {"message": "x"}),
            ],
        },
        {"role": "assistant", "content": "好了。"},
    ]

    async def fake_complete(messages, tools=None, **kwargs):  # noqa: ARG001
        return replies.pop(0)

    monkeypatch.setattr("app.agent.companion.complete_chat_message", fake_complete)

    result = await respond(
        "查天气放戏再告诉我儿子",
        system_prompt="你是康伴",
        ctx=ToolContext(elder_id="e1"),
        registry=_registry(order),
    )
    assert order == ["weather_get", "media_play"]
    assert "notify_family" not in result.tool_names


async def test_tool_loop_guard_stops_second_call(monkeypatch):
    order: list[str] = []
    replies = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("c1", "weather_get"), _call("c2", "weather_get")],
        }
    ]

    async def fake_complete(messages, tools=None, **kwargs):  # noqa: ARG001
        return replies.pop(0)

    monkeypatch.setattr("app.agent.companion.complete_chat_message", fake_complete)

    result = await respond(
        "查天气再查天气",
        system_prompt="你是康伴",
        ctx=ToolContext(),
        registry=_registry(order),
        guard=ToolLoopGuard(max_calls=1),
    )
    assert result.degraded is not None and result.degraded.startswith("tool_loop_guard")
    assert order == ["weather_get"]


async def test_empty_final_reply_raises(monkeypatch):
    async def fake_complete(messages, tools=None, **kwargs):  # noqa: ARG001
        return {"role": "assistant", "content": ""}

    monkeypatch.setattr("app.agent.companion.complete_chat_message", fake_complete)

    with pytest.raises(AgentUnavailable):
        await respond(
            "你好",
            system_prompt="你是康伴",
            ctx=ToolContext(),
            registry=_registry([]),
        )
