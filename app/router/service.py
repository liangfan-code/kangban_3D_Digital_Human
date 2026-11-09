"""三级路由入口：安全一票否决 → 规则层 → 分类器层 → 主控 Agent。

本函数是纯判定，不产生副作用，语音/文字通道与离线评测共用。
分类器只回答五组问题；是否单步执行、是否澄清、是否升级由这里决定。
"""

from __future__ import annotations

import logging

from app.router.classifier import (
    DIRECT_INTENTS,
    ClassifierUnavailable,
    IntentClassifier,
    LocalIntentClassifier,
    default_classifier,
)
from app.router.rules import extract_genre, match_rule, normalize_text
from app.router.safety import match_sensitive
from app.router.schemas import ClassifierResult, Intent, RouteDecision, RouteLayer
from app.system_one.questions import CLARIFY_QUESTION, confidence_threshold

logger = logging.getLogger(__name__)


def _media_control_action(text: str) -> str | None:
    """把媒体控制类自然语言映射到确定性动作；无法判断则升级主控。"""
    normalized = normalize_text(text)
    if any(
        word in normalized
        for word in ("小声", "小点", "太吵", "太响", "太大", "声音大", "音量小")
    ):
        return "volume_down"
    if any(word in normalized for word in ("大声", "大点", "听不清", "声音小", "音量大")):
        return "volume_up"
    if any(word in normalized for word in ("换台", "换个", "换一段", "下一个")):
        return "media_next"
    if any(word in normalized for word in ("停", "别放")):
        return "media_stop"
    return None


def _direct_target(
    intent: Intent, text: str
) -> tuple[str | None, dict[str, str], str | None]:
    """分类器高置信时的一步动作：返回 (tool, tool_args, action)。"""
    if intent is Intent.WEATHER:
        return "weather_get", {}, None
    if intent is Intent.MEDICATION_QUERY:
        return "medication_query", {}, None
    if intent is Intent.MEDIA_PLAY:
        genre = extract_genre(text)
        return "media_play", ({"genre": genre} if genre else {}), None
    if intent is Intent.MEDIA_CONTROL:
        return None, {}, _media_control_action(text)
    return None, {}, None


def _classify(text: str, classifier: IntentClassifier | None) -> ClassifierResult:
    provider = classifier or default_classifier()
    try:
        return provider.classify(text)
    except ClassifierUnavailable as exc:
        # Provider 不可用必须可见降级，但本轮仍要给出判定
        logger.warning(
            "classifier unavailable, fallback to local",
            extra={
                "event": "system_one_fallback",
                "provider": provider.provider,
                "error": str(exc),
            },
        )
        return LocalIntentClassifier().classify(text)


def route_text(
    text: str, classifier: IntentClassifier | None = None
) -> RouteDecision:
    """返回本轮路由判定；分类器低置信/域外时先澄清，其余升级主控 Agent。"""
    if not text or not text.strip():
        return RouteDecision(
            layer=RouteLayer.AGENT, intent=Intent.OTHER, escalated_to_agent=True
        )

    sensitive = match_sensitive(text)
    if sensitive is not None:
        return RouteDecision(
            layer=RouteLayer.SAFETY,
            intent=Intent.OTHER,
            is_sensitive=True,
            sensitive_category=sensitive.category,
        )

    rule_hit = match_rule(text)
    if rule_hit is not None:
        rule = rule_hit.rule
        return RouteDecision(
            layer=RouteLayer.RULE,
            intent=rule.intent,
            rule_id=rule.id,
            action=rule.action,
            tool=rule.tool,
            tool_args={"genre": rule_hit.genre} if rule_hit.genre else {},
        )

    result = _classify(text, classifier)

    if result.is_sensitive:
        hit = match_sensitive(text)
        return RouteDecision(
            layer=RouteLayer.SAFETY,
            intent=result.intent,
            confidence=result.confidence,
            is_sensitive=True,
            is_private=result.is_private,
            sensitive_category=hit.category if hit else "模型判定",
        )

    if result.is_out_of_domain:
        return RouteDecision(
            layer=RouteLayer.CLASSIFIER,
            intent=result.intent,
            confidence=result.confidence,
            is_private=result.is_private,
            needs_clarification=True,
        )

    if result.intent in DIRECT_INTENTS and result.confidence >= confidence_threshold():
        tool, tool_args, action = _direct_target(result.intent, text)
        if tool is not None or action is not None:
            return RouteDecision(
                layer=RouteLayer.CLASSIFIER,
                intent=result.intent,
                confidence=result.confidence,
                is_private=result.is_private,
                tool=tool,
                tool_args=tool_args,
                action=action,
            )

    if result.intent in DIRECT_INTENTS and result.confidence < confidence_threshold():
        # 低置信：先澄清，不猜着执行
        return RouteDecision(
            layer=RouteLayer.CLASSIFIER,
            intent=result.intent,
            confidence=result.confidence,
            is_private=result.is_private,
            needs_clarification=True,
        )

    # 闲聊/健康采集/无法一步执行：升级主控 Agent
    return RouteDecision(
        layer=RouteLayer.AGENT,
        intent=result.intent,
        confidence=result.confidence,
        is_private=result.is_private,
        escalated_to_agent=True,
    )


__all__ = ["CLARIFY_QUESTION", "route_text"]
