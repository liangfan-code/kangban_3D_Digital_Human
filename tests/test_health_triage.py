"""Day8 段1：健康分级纯规则测试（不连数据库、不经 LLM）。

覆盖：阈值边界、主诉关键词、红色优先、缺字段追问、措辞无诊断词。
"""

from app.services.health_triage import (
    DIAGNOSIS_WORDS,
    GREEN,
    RED,
    YELLOW,
    triage,
)


def _complete(**overrides):
    base = {
        "bp_systolic": 130,
        "bp_diastolic": 80,
        "sleep_minutes": 420,
        "mood": "很好",
        "chief_complaint": "没有不舒服",
    }
    base.update(overrides)
    return base


def test_normal_values_are_green_and_complete() -> None:
    result = triage(**_complete())
    assert result.level == GREEN
    assert result.reasons == ()
    assert result.missing_fields == ()
    assert result.advice


def test_systolic_boundary_179_yellow_180_red() -> None:
    assert triage(**_complete(bp_systolic=179)).level == YELLOW
    assert triage(**_complete(bp_systolic=180)).level == RED


def test_diastolic_boundary_109_yellow_110_red() -> None:
    assert triage(**_complete(bp_diastolic=109)).level == YELLOW
    assert triage(**_complete(bp_diastolic=110)).level == RED


def test_systolic_160_is_yellow() -> None:
    assert triage(**_complete(bp_systolic=160, bp_diastolic=90)).level == YELLOW


def test_diastolic_100_is_yellow() -> None:
    assert triage(**_complete(bp_systolic=140, bp_diastolic=100)).level == YELLOW


def test_sleep_boundary_299_yellow_300_green() -> None:
    assert triage(**_complete(sleep_minutes=299)).level == YELLOW
    assert triage(**_complete(sleep_minutes=300)).level == GREEN


def test_low_mood_is_yellow() -> None:
    for mood in ("很差", "低落", "烦躁", "睡不着"):
        assert triage(**_complete(mood=mood)).level == YELLOW


def test_red_complaint_keywords() -> None:
    for complaint in ("胸口有点痛，胸痛", "喘不上气", "刚才摔倒了", "意识不清"):
        assert triage(**_complete(chief_complaint=complaint)).level == RED


def test_red_takes_precedence_over_yellow() -> None:
    result = triage(
        **_complete(
            bp_systolic=185,
            bp_diastolic=95,
            sleep_minutes=120,
            mood="很差",
        )
    )
    assert result.level == RED


def test_missing_fields_are_reported_in_fixed_order() -> None:
    result = triage(bp_systolic=130, bp_diastolic=80)
    assert result.missing_fields == ("sleep_minutes", "mood", "chief_complaint")
    assert result.level == GREEN  # 已采集部分无异常，但不代表采集完成


def test_red_signal_still_escalates_when_fields_missing() -> None:
    result = triage(chief_complaint="胸闷得厉害")
    assert result.level == RED
    assert result.missing_fields  # 缺字段仍上报红色，避免因继续追问延误


def test_advice_contains_no_diagnosis_words() -> None:
    samples = [
        triage(**_complete()),
        triage(**_complete(bp_systolic=165)),
        triage(**_complete(chief_complaint="胸痛")),
        triage(bp_systolic=130),
    ]
    for result in samples:
        for word in DIAGNOSIS_WORDS:
            assert word not in result.advice, f"措辞含诊断词 {word}: {result.advice}"


def test_next_question_matches_first_missing_field() -> None:
    result = triage(bp_systolic=130)
    assert "低压" in result.advice
