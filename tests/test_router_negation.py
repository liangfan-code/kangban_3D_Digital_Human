"""P0-2：否定/时序优先，避免否定指令被当成正向执行或误升级 Agent。"""

from __future__ import annotations

import pytest

from app.agent.companion import looks_multi_intent
from app.router.executor import execute_rule
from app.router.schemas import Intent, RouteLayer
from app.router.service import route_text


@pytest.mark.parametrize(
    "text",
    ["别放越剧了", "别放戏了", "别播越剧了", "不要放戏", "我不想听戏", "我不听了"],
)
def test_negated_play_stops_media(text: str) -> None:
    decision = route_text(text)
    assert decision.layer is RouteLayer.RULE
    assert decision.action == "media_stop"
    assert looks_multi_intent(text) is False, "否定播放不得升级为多意图"


@pytest.mark.parametrize("text", ["不要放了", "关掉", "关了吧", "先暂停", "停一下"])
def test_plain_stop_commands_unchanged(text: str) -> None:
    decision = route_text(text)
    assert decision.layer is RouteLayer.RULE
    assert decision.action == "media_stop"


@pytest.mark.parametrize("text", ["我不服药", "我不吃药", "我不想吃药", "别吃药了"])
def test_refusing_medication_escalates_without_tool(text: str) -> None:
    decision = route_text(text)
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True
    assert decision.tool is None, "拒绝服药不得调用用药计划工具"


@pytest.mark.parametrize("text", ["先别放，等我吃完药", "等我吃完药再放"])
def test_deferred_play_is_not_stop(text: str) -> None:
    decision = route_text(text)
    assert decision.layer is RouteLayer.RULE
    assert decision.action == "media_defer"
    assert looks_multi_intent(text) is False

    outcome = execute_rule(decision)
    assert outcome is not None
    assert "先不放" in outcome.reply
    assert outcome.events == (), "延后指令不下发任何媒体控制事件"


def test_deferred_play_yields_no_control_event() -> None:
    outcome = execute_rule(route_text("先别放，等我吃完药"))
    assert outcome is not None
    assert not any(event.get("action") == "stop" for event in outcome.events)


@pytest.mark.parametrize("text", ["听戏然后呢", "天气然后呢", "顺便问一下几点了"])
def test_followup_phrases_do_not_escalate(text: str) -> None:
    assert looks_multi_intent(text) is False


def test_polite_prefix_still_hits_time_rule() -> None:
    decision = route_text("顺便问一下几点了")
    assert decision.layer is RouteLayer.RULE
    assert decision.action == "time_query"


@pytest.mark.parametrize("text", ["再告诉我一遍", "再给我说一遍"])
def test_repeat_variants_use_rule_layer(text: str) -> None:
    decision = route_text(text)
    assert decision.layer is RouteLayer.RULE
    assert decision.action == "repeat"


def test_genuine_multi_intent_still_escalates() -> None:
    assert looks_multi_intent("听戏，顺便问下明天天气") is True
    decision = route_text("听戏，顺便问下明天天气")
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True
    assert decision.intent is Intent.OTHER
    assert decision.multi_intent is True, "路由结果必须自带多意图判定"
    assert decision.notify_intent is False


@pytest.mark.parametrize(
    "text",
    ["给孩子打个电话", "跟我闺女说一声", "跟我儿子讲一下", "给我女儿捎句话"],
)
def test_notify_single_intent_escalates_to_agent(text: str) -> None:
    """单意图通知家属必须进 Agent（notify_family 是高危 Tool，需转人工确认）。"""
    decision = route_text(text)
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True
    assert decision.notify_intent is True
    assert decision.multi_intent is False


def test_repeat_is_not_notify_intent() -> None:
    decision = route_text("再告诉我一遍")
    assert decision.action == "repeat"
    assert decision.notify_intent is False
