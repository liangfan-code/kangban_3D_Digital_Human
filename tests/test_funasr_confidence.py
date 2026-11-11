"""FunASR 置信度解析：缺失时必须为 None，不得回落 0.0。"""

from app.integrations.funasr_ws import _extract_confidence


def test_missing_confidence_returns_none() -> None:
    confidence, source = _extract_confidence({"text": "你好", "mode": "2pass-offline"})

    assert confidence is None
    assert source == "unavailable"


def test_provider_confidence_parsed() -> None:
    confidence, source = _extract_confidence({"text": "你好", "confidence": 0.87})

    assert confidence == 0.87
    assert source == "provider"


def test_alternative_field_names_supported() -> None:
    assert _extract_confidence({"score": 0.72}) == (0.72, "provider")
    assert _extract_confidence({"ctc_confidence": "0.65"}) == (0.65, "provider")


def test_invalid_or_out_of_range_ignored() -> None:
    assert _extract_confidence({"confidence": "abc"}) == (None, "unavailable")
    assert _extract_confidence({"confidence": 87}) == (None, "unavailable")
    assert _extract_confidence({"confidence": 0.0}) == (0.0, "provider")
