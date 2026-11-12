"""Day7 段7 评测资产测试：fixtures 结构、指标口径、gate 退出码、报告章节。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evaluator():
    return _load("eval_day7")


@pytest.fixture(scope="module")
def gate():
    return _load("eval_gate")


def _scenario(sid: str, *, tools: list[str], outcome: str, tags: list[str] | None = None) -> dict:
    return {
        "id": sid,
        "name": sid,
        "turns": ["测试话术"],
        "expected": {
            "route_layer": "agent",
            "intent": "chitchat",
            "tools": tools,
            "outcome": outcome,
        },
        "tags": tags or [],
        "notes": "测试长者A",
    }


def _outcome(
    evaluator,
    scenario: dict,
    *,
    layer: str = "agent",
    actual_tools: list[str] | None = None,
    actual_outcome: str | None = None,
    sensitive: bool = False,
    human_takeover: bool = False,
    reasons: list[str] | None = None,
):
    turn = evaluator.TurnRecord(
        index=1,
        layer=layer,
        intent=scenario["expected"]["intent"],
        rule_id=None,
        tools=list(actual_tools if actual_tools is not None else scenario["expected"]["tools"]),
        sensitive=sensitive,
        simulated_ms=100.0,
    )
    return evaluator.ScenarioOutcome(
        scenario=scenario,
        turns=[turn],
        outcome=actual_outcome or scenario["expected"]["outcome"],
        human_takeover=human_takeover,
        degraded=False,
        simulated_ms=100.0,
        reasons=list(reasons or []),
    )


# ---------------- fixtures ----------------


def test_fixture_has_twenty_unique_scenarios(evaluator):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    assert len(scenarios) == 20
    assert len({item["id"] for item in scenarios}) == 20
    for item in scenarios:
        evaluator.validate_scenario(item)
        assert 1 <= len(item["turns"]) <= 3
        assert item["expected"]["route_layer"] in evaluator.ALLOWED_LAYERS
        assert item["expected"]["outcome"] in evaluator.ALLOWED_OUTCOMES
        assert isinstance(item["expected"]["tools"], list)
        assert isinstance(item["tags"], list)
        assert item["notes"].strip()


def test_fixture_covers_required_distribution(evaluator):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    tags = [tag for item in scenarios for tag in item["tags"]]
    assert tags.count("memory") == 4
    assert tags.count("medication") == 5
    assert tags.count("escalation") == 3
    assert tags.count("media") >= 4
    assert tags.count("multi_intent") == 3
    assert tags.count("safety") == 1
    assert tags.count("clarify") == 1
    assert tags.count("degraded") == 2


def test_fixture_uses_fictional_elders_only(evaluator):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    text = json.dumps(scenarios, ensure_ascii=False)
    for forbidden in ("身份证", "住院号", "手机号", "病史", "床号"):
        assert forbidden not in text
    assert "测试长者" in text


# ---------------- 指标 ----------------


def test_compute_metrics_on_known_outcomes(evaluator):
    scenarios = [
        _scenario("s-ok", tools=["memory_recall"], outcome="memory_referenced"),
        _scenario("s-tool-bad", tools=["media_play"], outcome="media_played"),
        _scenario("s-safe", tools=[], outcome="safety", tags=["safety"]),
        _scenario("s-hitl", tools=["notify_family"], outcome="hitl_notified", tags=["hitl"]),
    ]
    outcomes = [
        _outcome(evaluator, scenarios[0]),
        _outcome(evaluator, scenarios[1], actual_tools=["media_next"], reasons=["tools:mismatch"]),
        _outcome(evaluator, scenarios[2], layer="safety", sensitive=True, human_takeover=True),
        _outcome(evaluator, scenarios[3], human_takeover=True),
    ]
    metrics = evaluator.compute_metrics(outcomes)
    assert metrics["samples"] == 4
    assert metrics["task_success_rate"] == 0.75
    assert metrics["sensitive_recall"] == 1.0
    assert metrics["sensitive_denominator"] == 1
    assert metrics["tool_accuracy"] == pytest.approx(2 / 3)
    assert metrics["tool_denominator"] == 3
    assert metrics["human_takeover_rate"] == 0.5
    assert metrics["token_cost"] == 0
    assert metrics["p95_latency_ms"] is not None


def test_missing_tool_scenarios_report_no_ratio(evaluator):
    metrics = evaluator.compute_metrics(
        [_outcome(evaluator, _scenario("s-1", tools=[], outcome="clarify"))]
    )
    assert metrics["tool_accuracy"] is None
    assert metrics["sensitive_recall"] is None


# ---------------- gate ----------------


def _write_metrics(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_gate_passes_when_thresholds_met(gate, tmp_path):
    metrics = _write_metrics(
        tmp_path,
        {
            "samples": 20,
            "task_success_rate": 0.95,
            "sensitive_recall": 1.0,
            "tool_accuracy": 0.94,
        },
    )
    assert gate.main(["--metrics-json", str(metrics)]) == 0


def test_gate_fails_when_metric_below_threshold(gate, tmp_path):
    metrics = _write_metrics(
        tmp_path,
        {
            "samples": 20,
            "task_success_rate": 0.5,
            "sensitive_recall": 1.0,
            "tool_accuracy": 0.9,
        },
    )
    assert gate.main(["--metrics-json", str(metrics)]) == 1


def test_gate_fails_when_samples_insufficient(gate, tmp_path):
    metrics = _write_metrics(
        tmp_path,
        {
            "samples": 5,
            "task_success_rate": 1.0,
            "sensitive_recall": 1.0,
            "tool_accuracy": 1.0,
        },
    )
    assert gate.main(["--metrics-json", str(metrics)]) == 1


def test_gate_fails_when_metrics_missing(gate, tmp_path):
    report = tmp_path / "empty.md"
    report.write_text("# 无指标块\n", encoding="utf-8")
    assert gate.main(["--report", str(report)]) == 1


def test_gate_warn_only_returns_zero(gate, tmp_path):
    metrics = _write_metrics(
        tmp_path,
        {
            "samples": 20,
            "task_success_rate": 0.1,
            "sensitive_recall": 0.1,
            "tool_accuracy": 0.1,
        },
    )
    assert gate.main(["--metrics-json", str(metrics), "--warn-only"]) == 0


def test_gate_reads_metrics_from_report(gate, evaluator, tmp_path):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    outcomes = evaluator.asyncio.run(evaluator.evaluate(scenarios))
    metrics = evaluator.compute_metrics(outcomes)
    report = evaluator.render_markdown(
        outcomes,
        metrics,
        fixtures=str(evaluator.DEFAULT_FIXTURES),
        generated_at="2026-01-01 00:00:00",
        mode="offline",
    )
    path = tmp_path / "report.md"
    path.write_text(report, encoding="utf-8")
    assert gate.parse_report_metrics(report)["samples"] == 20
    assert gate.main(["--report", str(path)]) == 0


# ---------------- 报告 ----------------


def test_report_contains_required_sections(evaluator):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    outcomes = evaluator.asyncio.run(evaluator.evaluate(scenarios))
    metrics = evaluator.compute_metrics(outcomes)
    report = evaluator.render_markdown(
        outcomes,
        metrics,
        fixtures="eval/fixtures/day7_scenarios.jsonl",
        generated_at="2026-01-01 00:00:00",
        mode="offline",
    )
    for section in (
        "## 1. 核心指标（8 项）",
        "## 2. 分层分布",
        "## 3. 场景明细",
        "## 4. 失败分层归因",
        "## 5. 与 Day6 路由基线的关系",
        "## 6. 局限与复现",
        "<!-- eval_metrics",
        "- 模式：`offline`",
        "- 评测集：",
    ):
        assert section in report
    for scenario in scenarios:
        assert scenario["id"] in report
    for forbidden in ("身份证", "住院号", "手机号", "音频"):
        assert forbidden not in report


def test_report_metrics_come_from_computation(evaluator):
    scenarios = evaluator.load_scenarios(evaluator.DEFAULT_FIXTURES)
    outcomes = evaluator.asyncio.run(evaluator.evaluate(scenarios))
    metrics = evaluator.compute_metrics(outcomes)
    report = evaluator.render_markdown(
        outcomes,
        metrics,
        fixtures="eval/fixtures/day7_scenarios.jsonl",
        generated_at="2026-01-01 00:00:00",
        mode="offline",
    )
    block = report.split("<!-- eval_metrics ", 1)[1].split(" -->", 1)[0]
    payload = json.loads(block)
    assert payload["samples"] == metrics["samples"]
    assert payload["task_success_rate"] == metrics["task_success_rate"]
    assert payload["tool_accuracy"] == metrics["tool_accuracy"]
