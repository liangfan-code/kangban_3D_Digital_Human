"""Day8 段4：声纹阈值评测（FAR/FRR/EER）+ 管理端只读报告接口。"""

import json

import pytest

from app.core.config import get_settings
from scripts.voiceprint_eval import build_scores, evaluate


class FakeVoiceprintProvider:
    """按音频首字节返回固定向量，便于验证阈值统计。"""

    def __init__(self) -> None:
        self.table = {
            b"a": [1.0, 0.0],
            b"b": [0.8, 0.6],
            b"c": [0.0, 1.0],
        }

    def extract_embedding(self, audio_16k: bytes) -> list[float]:
        key = audio_16k[:1]
        if key not in self.table:
            raise ValueError("样本无效")
        return self.table[key]

    @staticmethod
    def cosine(a, b) -> float:
        return sum(x * y for x, y in zip(a, b))

    def model_fingerprint(self) -> str:
        return "fake-model"


def test_evaluate_separable_samples_has_zero_error() -> None:
    report = evaluate([0.91, 0.86, 0.21, 0.12], [True, True, False, False])
    best = next(r for r in report["threshold_table"] if r["threshold"] == 0.5)
    assert best["far"] == 0.0
    assert best["frr"] == 0.0
    assert best["accuracy"] == 1.0
    assert report["eer"]["value"] == 0.0
    assert report["sample_count"] == {
        "pairs": 4,
        "same_speaker": 2,
        "different_speaker": 2,
    }


def test_evaluate_reports_far_and_frr_per_threshold() -> None:
    # 正样本 0.60 / 0.80；负样本 0.55 / 0.70
    report = evaluate([0.60, 0.80, 0.55, 0.70], [True, True, False, False])
    row = next(r for r in report["threshold_table"] if r["threshold"] == 0.65)
    assert row["far"] == 0.5  # 0.70 被误判为同一人
    assert row["frr"] == 0.5  # 0.60 被漏判
    assert row["accuracy"] == 0.5


def test_threshold_table_covers_050_to_090() -> None:
    report = evaluate([0.5], [True])
    thresholds = [row["threshold"] for row in report["threshold_table"]]
    assert thresholds[0] == 0.5
    assert thresholds[-1] == 0.9
    assert thresholds == sorted(thresholds)
    assert len(thresholds) == 9


def test_build_scores_skips_broken_pairs(tmp_path) -> None:
    (tmp_path / "a.pcm").write_bytes(b"a")
    (tmp_path / "b.pcm").write_bytes(b"b")
    (tmp_path / "c.pcm").write_bytes(b"c")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "pairs": [
                    {
                        "elder_id": "e1",
                        "same_speaker": True,
                        "audio_a": "a.pcm",
                        "audio_b": "b.pcm",
                    },
                    {
                        "elder_id": "e1",
                        "same_speaker": False,
                        "audio_a": "a.pcm",
                        "audio_b": "c.pcm",
                    },
                    {
                        "elder_id": "e2",
                        "same_speaker": True,
                        "audio_a": "a.pcm",
                        "audio_b": "b.pcm",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.pcm").write_bytes(b"x")  # 无效样本 → 该对被跳过

    scores, labels, skipped = build_scores(manifest, FakeVoiceprintProvider())

    # 第 1、3 对含无效样本 b→x，跳过；第 2 对正常计分
    assert skipped == 2
    assert scores == [0.0]
    assert labels == [False]


async def test_latest_report_returns_newest_file(
    api, seed, tmp_path, auth_headers_factory, monkeypatch
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "voiceprint_eval_20260101T000000Z.json").write_text(
        json.dumps({"recommended_threshold": 0.6}), encoding="utf-8"
    )
    (report_dir / "voiceprint_eval_20260202T000000Z.json").write_text(
        json.dumps({"recommended_threshold": 0.75}), encoding="utf-8"
    )
    monkeypatch.setattr(
        get_settings(), "voiceprint_eval_report_dir", str(report_dir)
    )

    resp = await api.get(
        "/api/v1/admin/voiceprint-eval/latest",
        headers=await auth_headers_factory("admin"),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["recommended_threshold"] == 0.75


async def test_latest_report_404_when_not_evaluated(
    api, seed, tmp_path, auth_headers_factory, monkeypatch
) -> None:
    monkeypatch.setattr(
        get_settings(), "voiceprint_eval_report_dir", str(tmp_path / "empty")
    )
    resp = await api.get(
        "/api/v1/admin/voiceprint-eval/latest",
        headers=await auth_headers_factory("admin"),
    )
    assert resp.status_code == 404


async def test_latest_report_forbidden_for_nurse(
    api, seed, auth_headers_factory
) -> None:
    resp = await api.get(
        "/api/v1/admin/voiceprint-eval/latest",
        headers=await auth_headers_factory("nurse_a"),
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["doctor", "family"])
async def test_latest_report_forbidden_for_other_roles(
    api, seed, auth_headers_factory, role
) -> None:
    resp = await api.get(
        "/api/v1/admin/voiceprint-eval/latest",
        headers=await auth_headers_factory(role),
    )
    assert resp.status_code == 403
