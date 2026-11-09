"""Day6.4 评测脚本测试：加载、指标、回流文件与报告渲染。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_day6.py"


@pytest.fixture(scope="module")
def evaluator():
    spec = importlib.util.spec_from_file_location("eval_day6", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["eval_day6"] = module
    spec.loader.exec_module(module)
    return module


def test_fixture_has_fifty_unique_samples(evaluator):
    samples = evaluator.load_samples(evaluator.DEFAULT_FIXTURES)
    assert len(samples) == 50
    assert len({item["id"] for item in samples}) == 50
    assert {item["expected_layer"] for item in samples} == {
        "safety",
        "rule",
        "classifier",
        "agent",
    }


def test_baseline_fixture_passes_all_checks(evaluator):
    samples = evaluator.load_samples(evaluator.DEFAULT_FIXTURES)
    outcomes = evaluator.evaluate(samples)
    metrics = evaluator.compute_metrics(outcomes)
    assert metrics["failures"] == []
    assert metrics["sensitive_recall"] == 1.0
    assert metrics["layer_accuracy"] == 1.0
    assert metrics["error_count"] == 0


def test_mismatch_becomes_badcase_then_clears(evaluator, tmp_path):
    samples = [
        {
            "id": "probe-1",
            "text": "我想听越剧",
            "expected_layer": "agent",
            "expected_intent": "media_play",
            "expected_sensitive": False,
        }
    ]
    outcomes = evaluator.evaluate(samples)
    metrics = evaluator.compute_metrics(outcomes)
    assert len(metrics["failures"]) == 1
    assert any(reason.startswith("layer:") for reason in metrics["failures"][0].reasons)

    target = tmp_path / "badcases.jsonl"
    count = evaluator.write_badcases(metrics["failures"], target, "2026-01-01 00:00:00")
    assert count == 1
    assert target.exists()
    assert "probe-1" in target.read_text(encoding="utf-8")

    assert evaluator.write_badcases([], target, "2026-01-01 00:00:00") == 0
    assert not target.exists()


def test_report_contains_required_sections(evaluator):
    samples = evaluator.load_samples(evaluator.DEFAULT_FIXTURES)
    outcomes = evaluator.evaluate(samples)
    metrics = evaluator.compute_metrics(outcomes)
    report = evaluator.render_markdown(
        outcomes,
        metrics,
        evaluator.cost_comparison(outcomes),
        "eval/fixtures/day6_intents.jsonl",
        "2026-01-01 00:00:00",
    )
    for heading in ("敏感召回率", "S1 闭环率", "成本对比", "bad case 回流", "验收结论"):
        assert heading in report
