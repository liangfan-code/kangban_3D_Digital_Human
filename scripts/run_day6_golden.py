"""Day6 黄金集基线报告：不改产品代码，只量化当前路由/安全/多意图的漏检。

用法：
    python scripts/run_day6_golden.py                 # dev + holdout
    python scripts/run_day6_golden.py --set holdout   # 只跑 holdout

退出码恒为 0（报告模式）；门禁阈值见 tests/test_day6_golden.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.router.classifier import LocalIntentClassifier  # noqa: E402
from app.router.safety import match_sensitive  # noqa: E402
from app.router.service import route_text  # noqa: E402

FIXTURES = ROOT / "eval" / "fixtures"


def load(name: str) -> list[dict]:
    path = FIXTURES / f"day6_golden_{name}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def check(item: dict) -> list[str]:
    text = item["text"]
    expect = item.get("expect", {})
    decision = route_text(text, classifier=LocalIntentClassifier())
    safety = match_sensitive(text)
    actual = {
        "layer": decision.layer.value,
        "intent": decision.intent.value,
        "action": decision.action,
        "tool": decision.tool,
        "safety": safety is not None,
        "multi_intent": decision.multi_intent,
        "notify_intent": decision.notify_intent,
        "clarify": decision.needs_clarification,
    }
    failures = []
    for key, want in expect.items():
        got = actual.get(key)
        if got != want:
            failures.append(f"{key}: 期望={want!r} 实际={got!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", choices=["dev", "holdout", "all"], default="all")
    args = parser.parse_args()

    names = ["dev", "holdout"] if args.set == "all" else [args.set]
    total = passed = 0
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    failures: list[tuple[str, str, list[str]]] = []

    for name in names:
        for item in load(name):
            total += 1
            bad = check(item)
            bucket = by_category[item["category"]]
            bucket[1] += 1
            if bad:
                failures.append((item["id"], item["text"], bad))
            else:
                passed += 1
                bucket[0] += 1

    print(f"黄金集基线（{'+'.join(names)}）：{passed}/{total} 通过，{total - passed} 未达预期\n")
    print(f"{'类别':<16}{'通过':>6}{'总数':>6}{'通过率':>9}")
    for category, (ok, all_) in sorted(by_category.items()):
        print(f"{category:<16}{ok:>6}{all_:>6}{ok / all_:>8.0%}")

    if failures:
        print("\n未达预期明细：")
        for item_id, text, bad in failures:
            print(f"- {item_id} 《{text}》 " + "; ".join(bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
