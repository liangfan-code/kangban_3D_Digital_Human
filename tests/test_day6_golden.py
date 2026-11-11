"""Day6 黄金集门禁：把人工评测阈值固化为 pytest。

阈值（用户确认的计划）：
- dev 60/60 全过；
- holdout ≥59/62（留 3 条真实语料容错；含 2 条 provider 分歧句）；
- 安全召回 10/10（当前扩充到 12 条，必须全过）；
- 否定误执行 0；
- 顺承误升级 ≤1；
- 通知单意图必须进入 Agent，且 notify_family Tool 已注册。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_day6_golden import check, load  # noqa: E402

from app.agent.tools import build_default_registry  # noqa: E402
from app.router.service import route_text  # noqa: E402

DEV = load("dev")
HOLDOUT = load("holdout")


def _failures(items: list[dict]) -> list[tuple[str, str, list[str]]]:
    return [(item["id"], item["text"], bad) for item in items if (bad := check(item))]


def test_fixture_sizes_are_locked() -> None:
    assert len(DEV) == 60
    assert len(HOLDOUT) == 62
    ids = [item["id"] for item in DEV + HOLDOUT]
    assert len(ids) == len(set(ids)), "样例 id 必须唯一"


def test_dev_passes_completely() -> None:
    failures = _failures(DEV)
    assert not failures, f"dev 未达预期：{failures}"


def test_holdout_meets_threshold() -> None:
    failures = _failures(HOLDOUT)
    assert len(failures) <= 3, f"holdout 通过率低于 59/62：{failures}"


def test_safety_recall_full() -> None:
    safety = [item for item in DEV + HOLDOUT if item["category"] == "安全"]
    assert len(safety) >= 10, "安全样例不得少于 10 条"
    failures = _failures(safety)
    assert not failures, f"安全召回不足：{failures}"


def test_negation_never_executes() -> None:
    negated = [item for item in DEV + HOLDOUT if item["category"] == "否定"]
    assert negated, "否定样例缺失"
    failures = _failures(negated)
    assert not failures, f"否定句被误执行：{failures}"


def test_connector_false_escalation_budget() -> None:
    connectors = [item for item in DEV + HOLDOUT if item["category"] == "顺承词"]
    failures = _failures(connectors)
    assert len(failures) <= 1, f"顺承词误升级超过 1 条：{failures}"


def test_notify_single_intent_requires_agent_and_tool() -> None:
    assert "notify_family" in build_default_registry().names()
    notify = [item for item in DEV + HOLDOUT if item["category"] == "通知"]
    assert notify, "通知样例缺失"
    for item in notify:
        decision = route_text(item["text"])
        assert decision.notify_intent, f"未识别为通知意图：{item['id']}"
        assert decision.layer.value == "agent", f"通知未升级 Agent：{item['id']}"
        assert item.get("expect", {}).get("tool", "notify_family") == "notify_family"
