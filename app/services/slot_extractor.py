"""结构化槽位抽取（Day4.4）：仅做确定性规则，不额外调用模型。

覆盖三类高频意图：
- 用药确认（药名/时间/确认状态）
- 戏曲/媒体控制（动作/关键词）
- 身份确认（候选称呼/确认状态）

槽位只写入会话状态供 prompt 与后续工具层使用，**不执行任何动作**，也
**永不修改 verified_elder_id**（身份仍由服务端设备/声纹决定）。
"""

from __future__ import annotations

import re

from app.services.session_state import SessionSlots

_IDENTITY_RE = re.compile(r"我是([\u4e00-\u9fff]{2,4})")
_DRUG_RE = re.compile(r"([\u4e00-\u9fff]{1,10})(药|片|胶囊)")
_TIME_RE = re.compile(r"([零一二三四五六七八九十两0-9]{1,3})\s*(点|时|[:：])\s*([0-9]{2})?")
_DRUG_VERBS = "吃服喝用含贴"
_DRUG_EMPTY = {"什么", "啥", "哪种", "哪一种"}

_MEDICATION_WORDS = ("药", "服药", "用药", "提醒")
_MEDIA_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("换一段", "换一个", "换首", "下一段", "下一个"), "next"),
    (("暂停",), "pause"),
    (("别放了", "不听了", "关掉", "停止"), "stop"),
    (("大声", "声音大点", "响一点"), "volume_up"),
    (("小声", "声音小点", "轻一点"), "volume_down"),
    (("播放", "放一段", "来一段", "放个", "来首"), "play"),
)
_MEDIA_KEYWORDS = ("戏曲", "京剧", "评剧", "黄梅戏", "评书", "相声", "音乐")

_CONFIRM_TEXTS = {
    "好",
    "好的",
    "好呀",
    "对",
    "对的",
    "是",
    "是的",
    "是啊",
    "确认",
    "可以",
    "行",
    "吃过了",
}
_NEGATION_WORDS = (
    "不用",
    "不要",
    "取消",
    "等会",
    "等会儿",
    "算了",
    "不想",
    "先不",
    "不吃了",
    "没吃",
)


def _clean(text: str) -> str:
    return text.strip().strip("。！？!?，,、 ")


def _is_confirm(text: str) -> bool:
    return _clean(text) in _CONFIRM_TEXTS


def _is_negation(text: str) -> bool:
    return any(word in text for word in _NEGATION_WORDS)


def _media_action(text: str) -> tuple[str | None, str | None]:
    for keywords, action in _MEDIA_RULES:
        if any(word in text for word in keywords):
            return action, next((k for k in _MEDIA_KEYWORDS if k in text), None)
    keyword = next((k for k in _MEDIA_KEYWORDS if k in text), None)
    if keyword:
        return "play", keyword
    return None, None


def _medication(text: str) -> tuple[str | None, str | None]:
    drug_match = _DRUG_RE.search(text)
    drug = None
    if drug_match:
        prefix = drug_match.group(1)
        # 截到最近一个服药动词之后，避免「提醒我八点吃」被并进药名
        cut = max(prefix.rfind(verb) for verb in _DRUG_VERBS)
        name = prefix[cut + 1 :]
        if name and name not in _DRUG_EMPTY:
            drug = name + drug_match.group(2)
    time_match = _TIME_RE.search(text)
    time_value = None
    if time_match:
        time_value = f"{time_match.group(1)}{time_match.group(2)}{time_match.group(3) or ''}"
    return drug, time_value


def extract_slots(text: str, previous: SessionSlots) -> SessionSlots:
    """抽取本轮槽位并与上一轮合并；未识别到的字段保持不变。"""
    slots = previous.model_copy(deep=True)
    text = _clean(text)
    if not text:
        return slots

    identity_match = _IDENTITY_RE.search(text)
    medication_hit = any(word in text for word in _MEDICATION_WORDS)
    media_action, media_keyword = _media_action(text)

    if identity_match:
        slots.intent = "identity"
        slots.identity.candidate = identity_match.group(1).rstrip("啊呀哦呢吧")
    elif medication_hit:
        slots.intent = "medication"
        drug, time_value = _medication(text)
        if drug:
            slots.medication.drug = drug
        if time_value:
            slots.medication.time = time_value
        if _is_negation(text):
            slots.medication.confirmed = False
        elif _is_confirm(text):
            slots.medication.confirmed = True
    elif media_action:
        slots.intent = "media"
        slots.media.action = media_action
        if media_keyword:
            slots.media.keyword = media_keyword

    # 短确认/否定：跟随上一轮意图
    if not identity_match and not medication_hit and not media_action:
        if previous.intent == "medication" and (_is_confirm(text) or _is_negation(text)):
            slots.intent = "medication"
            slots.medication.confirmed = _is_confirm(text)
        elif previous.intent == "identity" and (_is_confirm(text) or _is_negation(text)):
            slots.intent = "identity"
            slots.identity.confirmed = _is_confirm(text)

    return slots
