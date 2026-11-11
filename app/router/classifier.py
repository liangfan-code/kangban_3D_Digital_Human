"""分类器层（Day6.2）：可插拔 System One Provider + 本地确定性兜底。

Provider 选择：
- ``SYSTEM_ONE_PROVIDER=laya`` 且本地 checkpoint 可用 → Laya（需预置权重，不自动下载）；
- 其余情况（含 Laya 缺失、加载失败、超时）→ 本地确定性分类器。

本地实现只做关键词加权打分，不是"模型"，作用是让三级路由在零依赖下可运行、
可测试、可离线评测；真实 Laya 接入后替换 Provider 即可，上层协议不变。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Protocol

from app.core.config import get_settings
from app.router.rules import normalize_text
from app.router.safety import match_sensitive
from app.router.schemas import ClassifierResult, Intent
from app.router.signals import MEDIA_GENRES
from app.system_one.questions import PRIVACY_KEYWORDS

logger = logging.getLogger(__name__)

# 直接单步执行的意图；闲聊/健康采集交给主控 Agent（需要上下文与多轮）
DIRECT_INTENTS: frozenset[Intent] = frozenset(
    {
        Intent.MEDICATION_QUERY,
        Intent.MEDIA_PLAY,
        Intent.MEDIA_CONTROL,
        Intent.WEATHER,
    }
)

# 媒体品类词与规则层同源（app.router.signals.MEDIA_GENRES）；
# 单字「戏/歌」只作规则层的动词宾语，不做关键词，避免「游戏/歌声」误命中。
_GENRE_WEIGHTS: Mapping[str, float] = {
    "戏曲": 0.8,
    "老歌": 0.8,
    "电影": 0.8,
    "唱段": 0.8,
}
_MEDIA_GENRE_KEYWORDS: tuple[tuple[str, float], ...] = tuple(
    (genre, _GENRE_WEIGHTS.get(genre, 1.0))
    for genre in MEDIA_GENRES
    if genre not in {"戏", "歌"}
)

# 关键词 -> 权重：强特征 1.0，弱特征 0.4~0.6
_KEYWORDS: Mapping[Intent, tuple[tuple[str, float], ...]] = {
    Intent.MEDICATION_QUERY: (
        ("降压药", 1.0),
        ("降糖药", 1.0),
        ("吃药", 1.0),
        ("服药", 1.0),
        ("药盒", 0.8),
        ("药片", 0.8),
        ("漏吃", 1.0),
        ("副作用", 0.8),
        ("几颗", 0.6),
        ("该吃", 0.6),
        ("药还", 0.6),
    ),
    Intent.MEDIA_PLAY: _MEDIA_GENRE_KEYWORDS,
    Intent.MEDIA_CONTROL: (
        ("声音", 0.6),
        ("音量", 0.8),
        ("太吵", 0.8),
        ("太响", 0.8),
        ("太大", 0.8),
        ("听不清", 0.8),
        ("换台", 1.0),
        ("换个", 0.6),
        ("换一段", 1.0),
        ("大声", 0.8),
        ("小声", 0.8),
        ("别放", 1.0),
        ("停", 0.6),
    ),
    Intent.WEATHER: (
        ("天气", 1.0),
        ("下雨", 1.0),
        ("下雪", 1.0),
        ("降温", 0.8),
        ("冷不冷", 1.0),
        ("热不热", 1.0),
        ("风大", 0.8),
        ("晒被子", 1.0),
        ("出门", 0.4),
    ),
    Intent.HEALTH_COLLECT: (
        ("血压", 1.0),
        ("血糖", 1.0),
        ("心率", 1.0),
        ("血氧", 1.0),
        ("体重", 1.0),
        ("量一下", 0.8),
        ("测一下", 0.8),
        ("测量", 0.8),
        ("体检", 0.8),
        ("膝盖疼", 0.8),
        ("腰疼", 0.8),
    ),
    Intent.CHITCHAT: (
        ("你好", 0.8),
        ("早上好", 0.8),
        ("晚上好", 0.8),
        ("陪我", 0.8),
        ("聊天", 0.8),
        ("想家", 0.8),
        ("孤单", 0.8),
        ("孙子", 0.6),
        ("孙女", 0.6),
        ("儿子", 0.6),
        ("女儿", 0.6),
        ("以前", 0.4),
        ("年轻", 0.4),
        ("吃饭", 0.4),
        ("睡觉", 0.4),
        ("谢谢", 0.6),
    ),
}

_OFF_TOPIC = (
    "股票",
    "基金",
    "编程",
    "代码",
    "法律",
    "合同",
    "诉讼",
    "翻译",
    "写文章",
    "政治",
    "火箭",
    "量子",
)


class ClassifierUnavailable(RuntimeError):
    """Provider 不可用（缺依赖、缺权重、超时）：上层必须可见降级到本地实现。"""


class IntentClassifier(Protocol):
    """System One 的统一协议；Provider 只回答五组问题，不执行动作。"""

    provider: str

    def classify(self, text: str) -> ClassifierResult: ...


def _score(text: str) -> dict[Intent, float]:
    scores: dict[Intent, float] = {}
    for intent, keywords in _KEYWORDS.items():
        total = sum(weight for keyword, weight in keywords if keyword in text)
        if total:
            scores[intent] = total
    return scores


def _confidence(top: float, second: float) -> float:
    """粗略标定：命中强度 + 与次优类的差距，单强特征约 0.88。"""
    return min(0.98, 0.60 + 0.10 * top + 0.18 * max(0.0, top - second))


class LocalIntentClassifier:
    """零依赖本地实现：关键词加权 + 差距置信度 + 隐私/域外规则。"""

    provider = "local"

    def classify(self, text: str) -> ClassifierResult:
        normalized = normalize_text(text)
        sensitive = match_sensitive(text)
        is_private = any(keyword in normalized for keyword in PRIVACY_KEYWORDS)

        if any(keyword in normalized for keyword in _OFF_TOPIC):
            return ClassifierResult(
                intent=Intent.OTHER,
                confidence=0.30,
                is_sensitive=sensitive is not None,
                is_private=is_private,
                is_out_of_domain=True,
                provider=self.provider,
            )

        scores = _score(normalized)
        if not scores:
            # 没有任务特征时默认闲聊：交给主控 Agent，不强行澄清
            return ClassifierResult(
                intent=Intent.CHITCHAT,
                confidence=0.50,
                is_sensitive=sensitive is not None,
                is_private=is_private,
                provider=self.provider,
            )

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_intent, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        return ClassifierResult(
            intent=top_intent,
            confidence=round(_confidence(top_score, second), 3),
            is_sensitive=sensitive is not None,
            is_private=is_private,
            provider=self.provider,
        )


_default: IntentClassifier | None = None


def _build_classifier() -> IntentClassifier:
    settings = get_settings()
    provider_name = (settings.system_one_provider or "").strip().lower()
    if provider_name in {"laya_http", "laya-serve"}:
        # 方案 C：权重在常驻 laya-serve 进程里加载，本进程只发 HTTP
        from app.integrations.laya_http import LayaHttpClassifier

        provider = LayaHttpClassifier(
            base_url=settings.system_one_base_url,
            model_name=settings.system_one_model_name,
            timeout_ms=settings.system_one_timeout_ms,
            api_key=settings.system_one_api_key,
        )
        if provider.available:
            return provider
        logger.warning(
            "system_one laya_http unavailable, fallback to local",
            extra={"event": "system_one_fallback", "provider": "laya_http"},
        )
        return LocalIntentClassifier()
    if provider_name == "laya":
        # 延迟导入：未安装 laya 时应用照常启动
        from app.integrations.laya import LayaClassifier

        provider = LayaClassifier(
            model_path=settings.system_one_model_path,
            timeout_ms=settings.system_one_timeout_ms,
        )
        if provider.available:
            return provider
        logger.warning(
            "system_one laya unavailable, fallback to local",
            extra={"event": "system_one_fallback", "provider": "laya"},
        )
    return LocalIntentClassifier()


def default_classifier() -> IntentClassifier:
    """进程级单例：Laya 加载成本高，不能每轮新建。"""
    global _default
    if _default is None:
        _default = _build_classifier()
    return _default


def reset_classifier() -> None:
    """测试用：清掉单例，重新按当前配置选择 Provider。"""
    global _default
    _default = None
