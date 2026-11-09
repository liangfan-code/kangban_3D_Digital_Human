"""安全一票否决（Day6）：先于规则层与任何模型。

原则（大纲课时 6.3）：
- 命中即进待人工确认队列（Day6.3 接入），只播预设安抚话术；
- 宁可错杀，不做医疗判断，不说「您这是××病」；
- 纯本地词表，零延迟、不依赖 LLM/分类器是否可用，模型层故障时依然生效。
"""

from __future__ import annotations

from dataclasses import dataclass

# 分类 → 关键词。只收明确的高危表达，避免把「天气难受」之类日常句误判；
# 业务侧仍按「宁可错杀」处理：命中即人工兜底。
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "急症",
        (
            "胸口疼",
            "胸口痛",
            "胸痛",
            "喘不上气",
            "喘不过气",
            "呼吸困难",
            "心口疼",
            "心脏难受",
            "中风",
            "半身麻",
            "说话不利索",
            "晕倒",
            "昏过去",
            "吐血",
        ),
    ),
    (
        "跌倒",
        ("摔了", "摔倒", "跌倒", "滑倒", "摔跤", "起不来", "爬不起来", "从床上掉"),
    ),
    (
        "走失",
        ("找不到家", "找不着家", "迷路", "走丢", "不知道这是哪", "回不去", "找不到房间"),
    ),
    (
        "情绪低落",
        ("不想活", "想死", "活着没意思", "没人管我", "不如死了", "不想活了"),
    ),
    (
        "身体不适",
        ("不舒服", "难受", "头晕", "恶心想吐", "疼得厉害", "浑身没劲"),
    ),
)


@dataclass(frozen=True)
class SensitiveHit:
    """命中的敏感类别与关键词（关键词只用于日志排查，不写原文）。"""

    category: str
    keyword: str


def match_sensitive(text: str) -> SensitiveHit | None:
    """返回首个命中的敏感类别；未命中返回 None。"""
    if not text:
        return None
    for category, keywords in _PATTERNS:
        for keyword in keywords:
            if keyword in text:
                return SensitiveHit(category=category, keyword=keyword)
    return None
