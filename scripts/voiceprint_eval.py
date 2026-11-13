"""离线声纹阈值评测（Day8 段4）。

输入：``data/voiceprint_eval/manifest.json``
    {"pairs": [{"elder_id": "...", "same_speaker": true,
                "audio_a": "a.wav", "audio_b": "b.wav"}, ...]}
    音频由现场自备（16kHz mono 16bit WAV/PCM），**不入库、不进日志**。

输出：``<report_dir>/voiceprint_eval_<UTC>.json``
    阈值 0.50~0.90 逐档 FAR / FRR / 准确率 + EER，供管理端只读展示。

退出码：0 正常；1 manifest 缺失/无有效样本；2 声纹 Provider 未配置。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许直接 python scripts/voiceprint_eval.py
    sys.path.insert(0, str(ROOT))

THRESHOLDS = [round(0.50 + 0.05 * i, 2) for i in range(9)]  # 0.50 ~ 0.90
DEFAULT_MANIFEST = ROOT / "data" / "voiceprint_eval" / "manifest.json"


def load_pcm(path: Path) -> bytes:
    """读取 16k mono 16bit 音频；WAV 去容器头，裸 PCM 原样返回。"""
    data = path.read_bytes()
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        idx = data.find(b"data")
        if idx == -1:
            raise ValueError("wav 缺少 data 块")
        return data[idx + 8 :]
    return data


def evaluate(scores: Sequence[float], labels: Sequence[bool]) -> dict[str, Any]:
    """按阈值逐档计算 FAR / FRR / 准确率，并给出 EER 与推荐阈值。"""
    positives = [s for s, same in zip(scores, labels) if same]
    negatives = [s for s, same in zip(scores, labels) if not same]
    total = len(scores)
    if total == 0:
        raise ValueError("没有可评测样本")

    table = []
    best: tuple[float, float, float, float] | None = None
    for threshold in THRESHOLDS:
        false_accept = (
            sum(1 for s in negatives if s >= threshold) / len(negatives)
            if negatives
            else 0.0
        )
        false_reject = (
            sum(1 for s in positives if s < threshold) / len(positives)
            if positives
            else 0.0
        )
        correct = sum(
            1
            for s, same in zip(scores, labels)
            if (s >= threshold) == bool(same)
        )
        table.append(
            {
                "threshold": threshold,
                "far": round(false_accept, 4),
                "frr": round(false_reject, 4),
                "accuracy": round(correct / total, 4),
            }
        )
        gap = abs(false_accept - false_reject)
        if best is None or gap < best[0]:
            best = (gap, threshold, false_accept, false_reject)

    assert best is not None
    _, eer_threshold, far, frr = best
    return {
        "threshold_table": table,
        "eer": {
            "value": round((far + frr) / 2, 4),
            "at_threshold": eer_threshold,
        },
        "recommended_threshold": eer_threshold,
        "sample_count": {
            "pairs": total,
            "same_speaker": len(positives),
            "different_speaker": len(negatives),
        },
    }


def build_scores(
    manifest_path: Path, provider
) -> tuple[list[float], list[bool], int]:
    """逐对提向量并计算余弦相似度；单对失败只跳过并计数。"""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    pairs = payload.get("pairs") or []
    base = manifest_path.parent
    scores: list[float] = []
    labels: list[bool] = []
    skipped = 0
    for pair in pairs:
        try:
            audio_a = load_pcm(base / pair["audio_a"])
            audio_b = load_pcm(base / pair["audio_b"])
            vec_a = provider.extract_embedding(audio_a)
            vec_b = provider.extract_embedding(audio_b)
            scores.append(float(provider.cosine(vec_a, vec_b)))
            labels.append(bool(pair["same_speaker"]))
        except (KeyError, OSError, ValueError):
            skipped += 1
    return scores, labels, skipped


def write_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"voiceprint_eval_{stamp}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="声纹阈值离线评测（FAR/FRR/EER）")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--report-dir", default=str(ROOT / "data" / "reports"), help="报告输出目录"
    )
    args = parser.parse_args(argv)

    manifest = Path(args.manifest)
    if not manifest.is_file():
        print(f"[voiceprint-eval] 未找到 manifest：{manifest}")
        print("请按 docs/Day8-改造方案.md 段4 准备 pairs（含 audio_a/audio_b）。")
        return 1

    from app.services.voice import get_voiceprint_provider

    try:
        provider = get_voiceprint_provider()
    except Exception as exc:  # noqa: BLE001 —— 配置缺失统一按未配置处理
        provider = None
        print(f"[voiceprint-eval] Provider 初始化失败：{type(exc).__name__}")
    if provider is None:
        print("[voiceprint-eval] 声纹 Provider 未配置（VOICEPRINT_MODEL_PATH），无法评测。")
        return 2

    scores, labels, skipped = build_scores(manifest, provider)
    if not scores:
        print("[voiceprint-eval] 没有可用样本（全部读取/提向量失败）。")
        return 1

    report = evaluate(scores, labels)
    report["generated_utc"] = datetime.now(UTC).isoformat()
    report["provider"] = provider.model_fingerprint()
    report["skipped_pairs"] = skipped
    out = write_report(report, Path(args.report_dir))

    print("阈值   FAR     FRR     准确率")
    for row in report["threshold_table"]:
        print(
            f"{row['threshold']:.2f}  {row['far']:.4f}  {row['frr']:.4f}  "
            f"{row['accuracy']:.4f}"
        )
    print(
        f"EER={report['eer']['value']:.4f} @ {report['eer']['at_threshold']:.2f}；"
        f"推荐阈值 {report['recommended_threshold']:.2f}；跳过 {skipped} 对"
    )
    print(f"报告已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
