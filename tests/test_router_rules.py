"""Day6.1 规则层与路由入口测试：10 条规则命中、负例、安全优先级、确定性执行。"""

from __future__ import annotations

import re

import pytest

from app.router.executor import execute_rule, safety_reply
from app.router.rules import match_rule, normalize_text
from app.router.safety import match_sensitive
from app.router.schemas import Intent, RouteLayer
from app.router.service import route_text


@pytest.mark.parametrize(
    ("text", "rule_id", "action", "intent"),
    [
        ("换一段。", "R01", "media_next", Intent.MEDIA_CONTROL),
        ("大声点", "R02", "volume_up", Intent.MEDIA_CONTROL),
        ("声音小一点", "R03", "volume_down", Intent.MEDIA_CONTROL),
        ("停", "R04", "media_stop", Intent.MEDIA_CONTROL),
        ("我想听越剧", "R05", "media_play", Intent.MEDIA_PLAY),
        ("现在几点了？", "R06", "time_query", Intent.OTHER),
        ("今天天气怎么样", "R07", "weather_query", Intent.WEATHER),
        ("我要吃药了", "R08", "medication_query", Intent.MEDICATION_QUERY),
        ("再见", "R09", "farewell", Intent.CHITCHAT),
        ("再说一遍", "R10", "repeat", Intent.OTHER),
    ],
)
def test_ten_rules_hit(text, rule_id, action, intent):
    hit = match_rule(text)
    assert hit is not None
    assert hit.rule.id == rule_id
    assert hit.rule.action == action
    assert hit.rule.intent is intent


def test_media_genre_is_extracted():
    hit = match_rule("我想听一段黄梅戏")
    assert hit is not None and hit.rule.id == "R05"
    assert hit.genre == "黄梅戏"


@pytest.mark.parametrize(
    "text",
    [
        "停车方便吗",  # 「停」不能误伤正常名词
        "我今天已经吃过药了",  # 已吃药是叙述，不是查询/提醒
        "我想听新闻",  # 没有媒体品类
        "我不喜欢大声说话",
        "你叫什么名字",
        "外面停车贵不贵",
    ],
)
def test_rule_negatives(text):
    assert match_rule(text) is None


def test_normalize_strips_punctuation_and_spaces():
    assert normalize_text(" 现 在 几 点 了 ？ ") == "现在几点了"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("我吃完药胸口疼", "急症"),
        ("我摔倒了起不来", "跌倒"),
        ("我找不到家了", "走失"),
        ("我不想活了", "情绪低落"),
        ("我有点不舒服", "身体不适"),
        # 词表覆盖不到的组合表达：慢病名 + 发作动词 / 部位 + 症状
        ("我感觉我心脏病犯了", "急症"),
        ("老毛病又犯了", "急症"),
        ("我犯心脏病了", "急症"),
        ("胸口像针扎一样", "急症"),
        ("半边身子发麻", "急症"),
    ],
)
def test_safety_lexicon(text, category):
    hit = match_sensitive(text)
    assert hit is not None and hit.category == category


@pytest.mark.parametrize("text", ["膝盖疼怎么办", "我孙子膝盖疼", "今天心情好多了"])
def test_safety_patterns_do_not_over_trigger(text):
    assert match_sensitive(text) is None


def test_safety_precedes_rules():
    decision = route_text("我要吃药，可是胸口疼")
    assert decision.layer is RouteLayer.SAFETY
    assert decision.is_sensitive is True
    assert decision.sensitive_category == "急症"
    assert decision.rule_id is None


def test_rule_layer_decision_carries_tool():
    decision = route_text("今天天气怎么样")
    assert decision.layer is RouteLayer.RULE
    assert decision.tool == "weather_get"


def test_multi_intent_escalates_before_rules():
    """多意图先于规则层：否则天气规则会截断「几点了」这半个问题。"""
    decision = route_text("今天天气怎么样，现在几点了？")
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True


def test_unmatched_text_escalates_to_agent():
    decision = route_text("你陪我聊聊天吧")
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True


def test_blank_text_escalates_to_agent():
    decision = route_text("   ")
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True


def test_execute_media_stop_emits_control():
    outcome = execute_rule(route_text("停"))
    assert outcome is not None
    assert outcome.reply == "好，我停下了。"
    assert outcome.events == ({"type": "control", "action": "stop"},)


def test_execute_volume_and_genre():
    up = execute_rule(route_text("大声点"))
    assert up is not None and up.events[0]["action"] == "volume_up"

    play = execute_rule(route_text("我想听京剧"))
    assert play is not None
    assert play.events[0] == {"type": "control", "action": "media_play", "genre": "京剧"}
    assert "京剧" in play.reply


def test_execute_time_reply_shape():
    outcome = execute_rule(route_text("现在几点了"))
    assert outcome is not None
    assert re.match(r"^现在是(上午|下午)\d{1,2}点\d{2}分。$", outcome.reply)


@pytest.mark.parametrize("text", ["今天天气怎么样", "我要吃药", "再说一遍"])
def test_tool_and_context_actions_defer_to_upper_layer(text):
    assert execute_rule(route_text(text)) is None


@pytest.mark.parametrize("category", ["急症", "跌倒", "走失", "情绪低落", "身体不适"])
def test_safety_reply_is_preset_and_non_diagnostic(category):
    reply = safety_reply(category)
    assert reply
    assert "病" not in reply  # 不做医疗诊断
