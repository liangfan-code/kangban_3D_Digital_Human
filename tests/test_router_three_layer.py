"""Day6 三级路由分层验收：约 30% 规则 / 50% 分类器 / 20% 主控 Agent。

数据集为人工构造的「可预期结果」样本，显式注入本地分类器，
不依赖 laya-serve 或任何外部服务，保证离线可重复。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.router.classifier import LocalIntentClassifier
from app.router.service import route_text

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "eval"
    / "fixtures"
    / "day6_three_layer.jsonl"
)
_ROWS: list[dict[str, Any]] = [
    json.loads(line)
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
_RULE_ROWS = [row for row in _ROWS if row["expected_layer"] == "rule"]
_CLASSIFIER_ROWS = [row for row in _ROWS if row["expected_layer"] == "classifier"]
_AGENT_ROWS = [row for row in _ROWS if row["expected_layer"] == "agent"]


def _decide(row: dict[str, Any]):
    return route_text(row["text"], classifier=LocalIntentClassifier())


def test_dataset_distribution_is_about_30_50_20() -> None:
    counts = Counter(row["expected_layer"] for row in _ROWS)

    assert len(_ROWS) == 40
    assert counts == {"rule": 12, "classifier": 20, "agent": 8}
    assert round(counts["rule"] / len(_ROWS) * 100) == 30
    assert round(counts["classifier"] / len(_ROWS) * 100) == 50
    assert round(counts["agent"] / len(_ROWS) * 100) == 20


def test_every_row_hits_expected_layer() -> None:
    mismatches = []
    for row in _ROWS:
        decision = _decide(row)
        if decision.layer.value != row["expected_layer"]:
            mismatches.append(
                f"{row['id']} {row['text']!r}: "
                f"expected={row['expected_layer']} actual={decision.layer.value}"
            )

    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("row", _RULE_ROWS, ids=[row["id"] for row in _RULE_ROWS])
def test_rule_rows_hit_declared_rule_and_action(row: dict[str, Any]) -> None:
    decision = _decide(row)

    assert decision.layer.value == "rule"
    assert decision.rule_id == row["expected_rule_id"]
    assert decision.intent.value == row["expected_intent"]
    assert decision.action == row["expected_action"]
    assert decision.tool == row["expected_tool"]


def test_all_ten_rules_are_covered() -> None:
    hit_rule_ids = {_decide(row).rule_id for row in _RULE_ROWS}

    assert hit_rule_ids == {f"R{index:02d}" for index in range(1, 11)}


@pytest.mark.parametrize(
    "row", _CLASSIFIER_ROWS, ids=[row["id"] for row in _CLASSIFIER_ROWS]
)
def test_rule_miss_falls_through_to_classifier(row: dict[str, Any]) -> None:
    decision = _decide(row)

    # 规则未命中：没有 rule_id，且落到分类器层
    assert decision.rule_id is None
    assert decision.layer.value == "classifier"
    assert decision.intent.value == row["expected_intent"]
    assert decision.needs_clarification is False
    assert decision.confidence is not None and decision.confidence >= 0.85
    assert (decision.tool, decision.action) == (
        row["expected_tool"],
        row["expected_action"],
    )


@pytest.mark.parametrize("row", _AGENT_ROWS, ids=[row["id"] for row in _AGENT_ROWS])
def test_rule_miss_falls_through_to_agent(row: dict[str, Any]) -> None:
    decision = _decide(row)

    assert decision.rule_id is None
    assert decision.layer.value == "agent"
    assert decision.escalated_to_agent is True
    assert decision.intent.value == row["expected_intent"]
