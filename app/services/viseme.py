"""TTS 音频 → 五口型时间轴（Day5.1，方案 B：能量 + 规则近似）。

不依赖厂商 viseme：对 16kHz mono s16le PCM 分窗，用 RMS（有声/静音）、
过零率与谱质心粗分 a/i/u/e/o，输出相对该段音频起点的毫秒区间。

口径（用户决策：选便宜的能跑通）：
- 只求「嘴型与语音大致对齐」，不追求音素级准确；
- 纯函数、无 IO、无日志；原始音频不出内存；
- 分类失败/静音一律不产出区间，前端自然闭嘴，不做假动作。
"""

from __future__ import annotations

import numpy as np

VISEMES = ("a", "i", "u", "e", "o")
DEFAULT_WINDOW_MS = 40
MIN_RMS = 0.02  # 归一化幅度（int16 / 32768）以下视为静音
MIN_SEGMENT_MS = 60  # 短于此的口型区间丢弃，避免抖动


def _features(chunk: np.ndarray, sample_rate: int) -> tuple[float, float, float]:
    """返回 (RMS, 过零率, 归一化谱质心)。"""
    rms = float(np.sqrt(np.mean(chunk * chunk)))
    signs = np.signbit(chunk)
    zcr = float(np.mean(signs[1:] != signs[:-1])) if chunk.size > 1 else 0.0
    windowed = chunk * np.hanning(chunk.size)
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(chunk.size, d=1.0 / sample_rate)
    total = float(spectrum.sum())
    centroid = float((spectrum * freqs).sum() / total) if total > 0 else 0.0
    return rms, zcr, centroid / (sample_rate / 2)


def _classify(rms: float, zcr: float, centroid: float) -> str | None:
    """元音粗分：静音→None；高频/高过零→i、e；大能量低频→a；低质心→u、o。"""
    if rms < MIN_RMS:
        return None
    if centroid >= 0.30 or zcr >= 0.40:
        return "i"
    if rms >= 0.075 and centroid < 0.22:
        return "a"
    if centroid >= 0.18 or zcr >= 0.22:
        return "e"
    if centroid < 0.10:
        return "u"
    return "o"


def build_viseme_timeline(
    pcm: bytes, sample_rate: int = 16000, *, window_ms: int = DEFAULT_WINDOW_MS
) -> list[dict]:
    """把 16k mono int16 PCM 转成 ``[{viseme, start_ms, end_ms}]``（按时间升序）。"""
    if sample_rate <= 0 or window_ms <= 0:
        raise ValueError("sample_rate/window_ms 必须为正")

    usable = len(pcm) // 2 * 2
    if usable == 0:
        return []
    samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float64) / 32768.0
    window = max(1, int(sample_rate * window_ms / 1000))
    total_ms = round(samples.size / sample_rate * 1000)

    segments: list[dict] = []
    current: str | None = None
    current_start = 0
    for start in range(0, samples.size, window):
        chunk = samples[start : start + window]
        start_ms = round(start / sample_rate * 1000)
        label = _classify(*_features(chunk, sample_rate)) if chunk.size else None
        if label == current:
            continue
        if current is not None and start_ms - current_start >= MIN_SEGMENT_MS:
            segments.append({"viseme": current, "start_ms": current_start, "end_ms": start_ms})
        current = label
        current_start = start_ms

    if current is not None and total_ms - current_start >= MIN_SEGMENT_MS:
        segments.append({"viseme": current, "start_ms": current_start, "end_ms": total_ms})
    return segments
