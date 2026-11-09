"""Day6.2 分类器层测试：本地实现、五组问题、置信度闸门、Laya 降级。"""

from __future__ import annotations

import types

import pytest

from app.integrations.laya import LayaClassifier
from app.router.classifier import (
    ClassifierUnavailable,
    LocalIntentClassifier,
    _build_classifier,
    reset_classifier,
)
from app.router.schemas import ClassifierResult, Intent, RouteLayer
from app.router.service import route_text
from app.system_one.questions import QUESTIONS_SPEC


def test_five_question_groups_are_defined():
    assert [name for name, _ in QUESTIONS_SPEC] == [
        "intent",
        "is_sensitive",
        "confidence",
        "is_private",
        "is_out_of_domain",
    ]


def test_local_classifier_returns_direct_intent_with_confidence():
    result = LocalIntentClassifier().classify("外面下雨了吗")
    assert result.intent is Intent.WEATHER
    assert result.confidence >= 0.85
    assert result.provider == "local"


def test_local_classifier_defaults_to_chitchat():
    result = LocalIntentClassifier().classify("你陪我聊聊天吧")
    assert result.intent is Intent.CHITCHAT
    assert result.is_out_of_domain is False


def test_local_classifier_detects_privacy():
    result = LocalIntentClassifier().classify("我的银行卡密码是多少")
    assert result.is_private is True


def test_local_classifier_detects_out_of_domain():
    result = LocalIntentClassifier().classify("股票今天涨了吗")
    assert result.intent is Intent.OTHER
    assert result.is_out_of_domain is True


def test_low_confidence_direct_intent_asks_for_clarification():
    decision = route_text("我想出门")
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.needs_clarification is True
    assert decision.escalated_to_agent is False


def test_out_of_domain_asks_for_clarification():
    decision = route_text("帮我写一段代码")
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.needs_clarification is True


def test_high_confidence_weather_maps_to_tool():
    decision = route_text("外面下雨了吗")
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.tool == "weather_get"
    assert decision.confidence is not None and decision.confidence >= 0.85


def test_high_confidence_media_play_carries_genre():
    decision = route_text("放个越剧听听")
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.tool == "media_play"
    assert decision.tool_args.get("genre") == "越剧"


def test_media_control_maps_to_action():
    decision = route_text("声音太大了")
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.action == "volume_down"


def test_chitchat_escalates_to_agent():
    decision = route_text("你陪我聊聊天吧")
    assert decision.layer is RouteLayer.AGENT
    assert decision.escalated_to_agent is True


def test_sensitive_classifier_result_becomes_safety_layer():
    decision = route_text("我从床上掉下来了")
    assert decision.layer is RouteLayer.SAFETY
    assert decision.sensitive_category == "跌倒"


def test_classifier_unavailable_falls_back_to_local():
    class Broken:
        provider = "broken"

        def classify(self, text: str) -> ClassifierResult:  # noqa: ARG002
            raise ClassifierUnavailable("boom")

    decision = route_text("降压药还有几颗", classifier=Broken())
    assert decision.layer is RouteLayer.CLASSIFIER
    assert decision.tool == "medication_query"
    assert decision.confidence is not None and decision.confidence >= 0.85


def test_laya_unavailable_without_package_or_checkpoint():
    provider = LayaClassifier(model_path=None)
    assert provider.available is False
    assert provider.unavailable_reason is not None
    with pytest.raises(ClassifierUnavailable):
        provider.classify("你好")


def test_build_classifier_falls_back_when_laya_missing(tmp_path, monkeypatch):
    settings = types.SimpleNamespace(
        system_one_provider="laya",
        system_one_model_path=str(tmp_path),  # 路径存在但 laya 包未安装
        system_one_timeout_ms=200,
        system_one_confidence_threshold=0.85,
    )
    monkeypatch.setattr("app.router.classifier.get_settings", lambda: settings)
    reset_classifier()
    try:
        assert isinstance(_build_classifier(), LocalIntentClassifier)
    finally:
        reset_classifier()
