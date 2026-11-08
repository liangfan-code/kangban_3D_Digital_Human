"""五口型时间轴测试：合成 PCM 驱动，不依赖 TTS 厂商。"""

import numpy as np
import pytest

from app.services.viseme import VISEMES, build_viseme_timeline


def _tone(seconds: float, freq: float = 250.0, amp: float = 0.3, rate: int = 16000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    data = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    return data.tobytes()


def _silence(seconds: float, rate: int = 16000) -> bytes:
    return np.zeros(int(seconds * rate), dtype="<i2").tobytes()


def test_empty_pcm_returns_empty():
    assert build_viseme_timeline(b"") == []


def test_silence_returns_empty():
    assert build_viseme_timeline(_silence(1.0)) == []


def test_tone_produces_valid_segments():
    segments = build_viseme_timeline(_tone(1.0))
    assert segments
    duration = 1000
    previous_end = 0
    for segment in segments:
        assert set(segment) == {"viseme", "start_ms", "end_ms"}
        assert segment["viseme"] in VISEMES
        assert segment["start_ms"] < segment["end_ms"]
        assert segment["start_ms"] >= previous_end
        assert segment["end_ms"] <= duration
        previous_end = segment["end_ms"]


def test_tone_silence_tone_has_gap():
    pcm = _tone(0.4) + _silence(0.4) + _tone(0.4)
    segments = build_viseme_timeline(pcm)
    assert len(segments) >= 2
    assert segments[0]["start_ms"] == 0
    assert segments[-1]["end_ms"] <= 1200
    # 中间静音：第一段结束与最后一段开始之间必须留出间隔
    assert segments[-1]["start_ms"] >= 400


def test_odd_byte_length_is_truncated():
    pcm = _tone(0.2) + b"\x00"
    assert build_viseme_timeline(pcm)


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        build_viseme_timeline(_tone(0.2), sample_rate=0)
    with pytest.raises(ValueError):
        build_viseme_timeline(_tone(0.2), window_ms=0)
