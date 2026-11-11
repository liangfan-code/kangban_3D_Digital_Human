"""三级路由的统一数据结构。

路由结果只描述「谁来回、调什么」，不执行副作用；执行由调用方（语音/文字通道）
或后续 Tool 层完成，便于离线评测直接复用同一套判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Intent(StrEnum):
    """业务意图标签（层次化：戏曲/电影归在娱乐点播下）。"""

    CHITCHAT = "chitchat"
    MEDICATION_QUERY = "medication_query"
    MEDIA_PLAY = "media_play"
    MEDIA_CONTROL = "media_control"
    WEATHER = "weather"
    HEALTH_COLLECT = "health_collect"
    OTHER = "other"


class RouteLayer(StrEnum):
    """命中层级；顺序即优先级。"""

    SAFETY = "safety"
    RULE = "rule"
    CLASSIFIER = "classifier"
    AGENT = "agent"


@dataclass(frozen=True)
class ClassifierResult:
    """System One 的统一输出：五组问题的答案，不掺业务副作用。"""

    intent: Intent
    confidence: float
    is_sensitive: bool = False
    is_private: bool = False
    is_out_of_domain: bool = False
    provider: str = "local"


@dataclass(frozen=True)
class RouteDecision:
    """一次路由判定。``action`` 是本地确定性动作名，``tool`` 是待调的 Tool 名。"""

    layer: RouteLayer
    intent: Intent = Intent.OTHER
    confidence: float | None = None
    is_sensitive: bool = False
    is_private: bool = False
    sensitive_category: str | None = None
    rule_id: str | None = None
    action: str | None = None
    tool: str | None = None
    tool_args: dict[str, str] = field(default_factory=dict)
    escalated_to_agent: bool = False
    needs_clarification: bool = False
    # 进入主控 Agent 的原因：调用方不得再用自己的词表二次判定。
    multi_intent: bool = False
    notify_intent: bool = False
