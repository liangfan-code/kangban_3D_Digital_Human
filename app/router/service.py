"""三级路由入口：安全一票否决 → 多意图升级 → 规则层 → 分类器层 → 主控 Agent。

本函数是纯判定，不产生副作用，语音/文字通道与离线评测共用。
分类器只回答五组问题；是否单步执行、是否澄清、是否升级由这里决定。
"""

from __future__ import annotations

import logging
import re

from app.agent.companion import looks_multi_intent
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
from app.router.signals import (
    detect_media_control,
    detect_medication_reply,
    is_deferred_play_command,
    is_denied_media_command,
    is_followup_question,
    is_media_evaluation,
    is_media_preference,
    is_negated_play_command,
    looks_notify_intent,
)
from app.system_one.questions import CLARIFY_QUESTION, confidence_threshold

logger = logging.getLogger(__name__)

# 低置信反问按意图定制：统一问句会让长者不知道该怎么回答。
_CLARIFY_BY_INTENT: dict[Intent, str] = {
    Intent.MEDIA_PLAY: "您想听哪一段？戏曲、老歌还是评书？",
    Intent.MEDIA_CONTROL: "您是想把声音调大一点，还是关小一点？",
    Intent.MEDICATION_QUERY: "您是想看看今天的用药计划吗？",
    Intent.WEATHER: "您想听今天还是明天的天气？",
}


def clarify_reply(decision: RouteDecision) -> str:
    return _CLARIFY_BY_INTENT.get(decision.intent, CLARIFY_QUESTION)


# 拒绝服药：「我不吃药」不是查询计划，不能调 medication_query 工具。
_NEGATED_MEDICATION_RE = re.compile(r"(?:不|不想|不要|不用|别)(?:再)?(?:吃|服|喝)药")


def _media_control_action(text: str) -> str | None:
    """把媒体控制类自然语言映射到确定性动作；无法判断则升级主控。"""
    return detect_media_control(normalize_text(text))


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

    # 否定/延后必须先于多意图与规则层：这些句子里「别放」+「越剧」会被算成
    # 两个需求而升级 Agent，或「别放越剧了」因整句锚定不中而落到分类器。
    if is_deferred_play_command(text):
        return RouteDecision(
            layer=RouteLayer.RULE,
            intent=Intent.MEDIA_CONTROL,
            rule_id="NEG-02",
            action="media_defer",
        )

    if is_negated_play_command(text):
        return RouteDecision(
            layer=RouteLayer.RULE,
            intent=Intent.MEDIA_CONTROL,
            rule_id="NEG-01",
            action="media_stop",
        )

    # 拒绝服药（「我不吃药」）不是查询计划，直接升级由主控 Agent 回应，
    # 绝不能调 medication_query 工具去念一遍用药计划。
    if _NEGATED_MEDICATION_RE.search(normalize_text(text)):
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=Intent.MEDICATION_QUERY,
            escalated_to_agent=True,
        )

    # 否认句（我没说要听戏）里虽然有「听戏」，但用户明确表示没提这个需求，
    # 不能执行点播；交给主控澄清/接住话题。
    if is_denied_media_command(text):
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=Intent.OTHER,
            escalated_to_agent=True,
        )

    # 追问（听戏然后呢）不是新指令：交给主控结合上下文回答，不能让规则层
    # 把前半句当成一次新的点播/查询去执行。
    if is_followup_question(text):
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=Intent.OTHER,
            escalated_to_agent=True,
        )

    # 多意图必须先于规则/分类器单步执行：否则「今天天气怎么样，现在几点了」
    # 会被 R07 天气规则截断，只回答其中一个问题（由主控 Agent 逐项调用工具）。
    if looks_multi_intent(text):
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=Intent.OTHER,
            escalated_to_agent=True,
            multi_intent=True,
        )

    # 服药确认/否认/延后（「我吃过药了」「还没吃」「等会儿吃」）必须早于规则层与
    # 分类器：否则「我吃过药了」会被当成查询计划重复念一遍药单。放在多意图之后，
    # 「吃完药再告诉我儿子」仍按多意图交给主控逐项处理。
    if detect_medication_reply(text) is not None:
        return RouteDecision(
            layer=RouteLayer.RULE,
            intent=Intent.MEDICATION_QUERY,
            rule_id="MED-01",
            tool="medication_confirm",
            tool_args={"reply": text},
        )

    # 单意图的通知家属也必须进 Agent：notify_family 是高危 Tool，只能由主控
    # 组织话术并转人工确认，不能当普通闲聊交给流式 LLM。intent 仍取分类器结果，
    # 通知只是升级原因，不硬改成 OTHER（避免同一句话换层就换意图标签）。
    if looks_notify_intent(text):
        result = _classify(text, classifier)
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=result.intent,
            confidence=result.confidence,
            is_private=result.is_private,
            escalated_to_agent=True,
            notify_intent=True,
        )

    # 评价句（这首越剧真好听 / 这戏唱得真好听）不是点播：分类器会因品类词
    # 给出高置信 media_play（外部 provider 尤其明显），这里统一交给主控闲聊接住。
    # 放在规则层之前，避免「听越剧真好听」这类语序被 R05 直接执行。
    if is_media_evaluation(normalize_text(text)):
        return RouteDecision(
            layer=RouteLayer.AGENT,
            intent=Intent.CHITCHAT,
            escalated_to_agent=True,
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

    # 「我爱听越剧」只是偏好表达，高置信也不能直接放戏：先澄清要不要现在放。
    if result.intent is Intent.MEDIA_PLAY and is_media_preference(normalize_text(text)):
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


__all__ = ["CLARIFY_QUESTION", "clarify_reply", "route_text"]
