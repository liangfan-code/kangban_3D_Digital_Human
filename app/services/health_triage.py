"""健康采集三级分级（Day8 段1）。

设计约束：
- 纯规则、确定性，不经 LLM：医疗相关判定不允许模型幻觉改判；
- 只输出「级别 + 结构化理由码 + 建议措辞」，措辞只做转述与建议求助，
  禁用诊断词（高血压/糖尿病等），由 ``tests/test_health_triage.py`` 断言；
- 缺字段时给下一问；但红色信号不因缺字段而降级，安全优先。
"""

from __future__ import annotations

from dataclasses import dataclass

GREEN = "green"
YELLOW = "yellow"
RED = "red"

LEVEL_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# 采集字段顺序即追问顺序（血压高低压成对，放在最前）
REQUIRED_FIELDS: tuple[str, ...] = (
    "bp_systolic",
    "bp_diastolic",
    "sleep_minutes",
    "mood",
    "chief_complaint",
)

QUESTIONS: dict[str, str] = {
    "bp_systolic": "您今天量血压了吗？高压是多少？",
    "bp_diastolic": "低压是多少呢？",
    "sleep_minutes": "昨晚大概睡了几个小时？",
    "mood": "今天心情怎么样？",
    "chief_complaint": "今天有没有哪里不舒服？",
}

RED_COMPLAINT_KEYWORDS: tuple[str, ...] = (
    "胸痛",
    "胸闷",
    "呼吸困难",
    "喘不上",
    "晕倒",
    "摔倒",
    "意识不清",
    "说不出话",
)

YELLOW_MOODS: frozenset[str] = frozenset({"很差", "低落", "烦躁", "睡不着"})

# 只用于测试白名单与文档说明：分级建议里不允许出现这些诊断性词汇
DIAGNOSIS_WORDS: tuple[str, ...] = (
    "高血压",
    "低血压",
    "糖尿病",
    "冠心病",
    "心梗",
    "心肌梗死",
    "脑梗",
    "中风",
    "低血糖",
    "感染",
)

_ADVICE = {
    GREEN: "今天的情况我记下了，您好好休息。",
    YELLOW: "今天的数据我记下了，我帮您告诉护工，让他们多留意一下。",
    RED: "这个情况我有点担心，我马上帮您叫工作人员来看看。",
}

# 理由码：只入 JSONB，不落长者原话
REASON_SYSTOLIC_RED = "systolic_ge_180"
REASON_DIASTOLIC_RED = "diastolic_ge_110"
REASON_COMPLAINT_RED = "complaint_critical"
REASON_SYSTOLIC_YELLOW = "systolic_160_179"
REASON_DIASTOLIC_YELLOW = "diastolic_100_109"
REASON_SLEEP_YELLOW = "sleep_lt_300"
REASON_MOOD_YELLOW = "mood_low"


@dataclass(frozen=True)
class TriageResult:
    """一次分级的可持久化结果。"""

    level: str
    reasons: tuple[str, ...]
    confidence: float
    advice: str
    missing_fields: tuple[str, ...]

    @property
    def needs_followup(self) -> bool:
        """是否需要继续追问；红色即使缺字段也直接上报。"""
        return bool(self.missing_fields) and self.level != RED


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _next_question(missing: tuple[str, ...]) -> str:
    return QUESTIONS.get(missing[0], "今天的情况我记下了。") if missing else ""


def triage(
    *,
    bp_systolic: object = None,
    bp_diastolic: object = None,
    sleep_minutes: object = None,
    mood: object = None,
    chief_complaint: object = None,
) -> TriageResult:
    """按确定性规则给出绿/黄/红；输入缺失不算异常值。"""
    systolic = _as_int(bp_systolic)
    diastolic = _as_int(bp_diastolic)
    sleep = _as_int(sleep_minutes)
    mood_text = _as_text(mood)
    complaint = _as_text(chief_complaint)

    reasons: list[str] = []
    level = GREEN

    # 红色：血压危急值或危急主诉，优先级最高
    if systolic is not None and systolic >= 180:
        level = RED
        reasons.append(REASON_SYSTOLIC_RED)
    if diastolic is not None and diastolic >= 110:
        level = RED
        reasons.append(REASON_DIASTOLIC_RED)
    if complaint is not None and any(word in complaint for word in RED_COMPLAINT_KEYWORDS):
        level = RED
        reasons.append(REASON_COMPLAINT_RED)

    if level != RED:
        if systolic is not None and 160 <= systolic <= 179:
            level = YELLOW
            reasons.append(REASON_SYSTOLIC_YELLOW)
        if diastolic is not None and 100 <= diastolic <= 109:
            level = YELLOW
            reasons.append(REASON_DIASTOLIC_YELLOW)
        if sleep is not None and sleep < 300:
            level = YELLOW
            reasons.append(REASON_SLEEP_YELLOW)
        if mood_text is not None and mood_text in YELLOW_MOODS:
            level = YELLOW
            reasons.append(REASON_MOOD_YELLOW)

    missing = tuple(
        name
        for name, value in (
            ("bp_systolic", systolic),
            ("bp_diastolic", diastolic),
            ("sleep_minutes", sleep),
            ("mood", mood_text),
            ("chief_complaint", complaint),
        )
        if value is None
    )

    advice = _next_question(missing) if missing and level != RED else _ADVICE[level]
    return TriageResult(
        level=level,
        reasons=tuple(reasons),
        confidence=1.0,
        advice=advice,
        missing_fields=missing,
    )


__all__ = [
    "DIAGNOSIS_WORDS",
    "GREEN",
    "LEVEL_RANK",
    "QUESTIONS",
    "RED",
    "RED_COMPLAINT_KEYWORDS",
    "REQUIRED_FIELDS",
    "TriageResult",
    "YELLOW",
    "YELLOW_MOODS",
    "triage",
]
