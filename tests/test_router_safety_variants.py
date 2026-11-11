"""P0-1：安全层口语变体召回与否定表述误报回归。"""

from __future__ import annotations

import pytest

from app.router.safety import match_sensitive
from app.router.schemas import RouteLayer
from app.router.service import route_text


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("站不稳", "跌倒"),
        ("站不住了", "跌倒"),
        ("腿发软", "跌倒"),
        ("两条腿发软", "跌倒"),
        ("说话有点不清楚", "急症"),
        ("说话有点不利索", "急症"),
        ("眼前一黑", "急症"),
        ("眼前一阵发黑", "急症"),
        ("半边身子麻", "急症"),
        ("半边身子发木", "急症"),
        ("嘴有点歪", "急症"),
        ("手抖得拿不住东西", "急症"),
        ("冒冷汗", "急症"),
        ("拿不住杯子", "急症"),
        ("头有点晕", "身体不适"),
        ("不太舒服", "身体不适"),
    ],
)
def test_safety_variants_are_detected(text: str, category: str) -> None:
    hit = match_sensitive(text)
    assert hit is not None, f"漏检：{text}"
    assert hit.category == category


@pytest.mark.parametrize(
    "text",
    [
        "我没有不舒服",
        "我没觉得难受",
        "我不头晕",
        "我不心慌",
        "我不发烧",
        "我没有哪里不舒服",
        "胸口不疼",
        "腿不麻",
    ],
)
def test_negated_symptoms_are_not_flagged(text: str) -> None:
    assert match_sensitive(text) is None, f"误报：{text}"


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("我没有不舒服，但是胸口疼", "急症"),
        ("我不头晕，可是站不稳", "跌倒"),
        ("头不晕，就是说话有点不清楚", "急症"),
    ],
)
def test_contrast_clause_still_detected(text: str, category: str) -> None:
    hit = match_sensitive(text)
    assert hit is not None and hit.category == category


def test_negated_symptom_does_not_enter_safety_layer() -> None:
    decision = route_text("我没有不舒服")
    assert decision.layer is not RouteLayer.SAFETY
    assert decision.is_sensitive is False
