"""结构化槽位抽取测试：用药 / 媒体 / 身份三类规则，不调用模型。"""

from app.services.session_state import SessionSlots
from app.services.slot_extractor import extract_slots


def test_medication_drug_and_time():
    slots = extract_slots("提醒我八点吃降压药", SessionSlots())
    assert slots.intent == "medication"
    assert slots.medication.drug == "降压药"
    assert slots.medication.time == "八点"


def test_medication_confirm_follows_previous_intent():
    previous = extract_slots("提醒我八点吃降压药", SessionSlots())
    slots = extract_slots("好", previous)
    assert slots.intent == "medication"
    assert slots.medication.drug == "降压药"
    assert slots.medication.confirmed is True


def test_medication_negation_sets_false():
    previous = extract_slots("提醒我八点吃降压药", SessionSlots())
    slots = extract_slots("先不吃了", previous)
    assert slots.intent == "medication"
    assert slots.medication.confirmed is False


def test_media_next_with_keyword():
    slots = extract_slots("换一段评书", SessionSlots())
    assert slots.intent == "media"
    assert slots.media.action == "next"
    assert slots.media.keyword == "评书"


def test_media_stop():
    slots = extract_slots("别放了", SessionSlots())
    assert slots.intent == "media"
    assert slots.media.action == "stop"


def test_identity_candidate():
    slots = extract_slots("我是王奶奶", SessionSlots())
    assert slots.intent == "identity"
    assert slots.identity.candidate == "王奶奶"


def test_empty_text_keeps_previous_slots():
    previous = extract_slots("换一段评书", SessionSlots())
    slots = extract_slots("   ", previous)
    assert slots.intent == "media"
    assert slots.media.action == "next"
    assert slots.media.keyword == "评书"


def test_unrelated_text_keeps_previous_slots():
    previous = extract_slots("换一段评书", SessionSlots())
    slots = extract_slots("今天天气不错", previous)
    assert slots.intent == "media"
    assert slots.media.action == "next"
