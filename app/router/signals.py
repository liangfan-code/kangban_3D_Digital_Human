"""路由/Agent/执行层共用的单一信号源（Day6 修复）。

修复前同一句话在不同层各有一套词表（``companion._SIGNALS`` 数多意图、
``rules.RULES`` 做规则命中、``service._media_control_action`` 判动作），
同义词只要漏在某一层就会系统性漏检。这里把所有信号收敛成一份：
规则层拼正则、多意图计数、执行动作都从这里取词。
"""

from __future__ import annotations

import re

# ---------------- 通知家属 ----------------

# 家属称谓：通知类请求必须明确指向某位家属，
# 否则「再告诉我一遍」这种重复请求会被误当成「联系家属」。
FAMILY_WORDS: tuple[str, ...] = (
    "孩子",
    "儿子",
    "女儿",
    "闺女",
    "孙子",
    "孙女",
    "老伴",
    "家人",
    "家属",
    "亲人",
)

NOTIFY_VERBS: tuple[str, ...] = (
    "打电话",
    "打个电话",
    "拨电话",
    "拨个电话",
    "说一声",
    "说句话",
    "讲一下",
    "讲一声",
    "捎句话",
    "捎个话",
    "发消息",
    "发个消息",
    "联系一下",
    "联系",
    "告诉",
    "通知",
    "转告",
)

_FAMILY = "(?:" + "|".join(FAMILY_WORDS) + ")"
_NOTIFY_VERB = "(?:" + "|".join(NOTIFY_VERBS) + ")"

# 两种自然语序都要认：称谓在前（给孩子打个电话）或动作在前（联系一下我儿子）。
# 中间只允许少量夹字，且不能跨句读，避免把前后两件事拼成一个通知请求。
_NOTIFY_RE = re.compile(
    rf"{_FAMILY}[^。！？，,；;]{{0,6}}{_NOTIFY_VERB}|"
    rf"{_NOTIFY_VERB}[^。！？，,；;]{{0,4}}{_FAMILY}"
)


def looks_notify_intent(text: str) -> bool:
    """是否在请求联系/告知某位家属（通知属高危动作，必须走 Agent + HITL）。"""
    return bool(_NOTIFY_RE.search(text or ""))


# ---------------- 媒体点播 ----------------

# 品类按「长词在前」排列：戏曲/黄梅戏要先于单字「戏」，老电影先于「电影」。
MEDIA_GENRES: tuple[str, ...] = (
    "越剧",
    "京剧",
    "黄梅戏",
    "沪剧",
    "评弹",
    "豫剧",
    "评书",
    "相声",
    "戏曲",
    "老歌",
    "红歌",
    "儿歌",
    "老电影",
    "电影",
    "邓丽君",
    "红楼梦",
    "唱段",
    "戏",
    "歌",
)
GENRE_ALT = "|".join(MEDIA_GENRES)
_GENRE_RE = re.compile("|".join(MEDIA_GENRES))

_PLAY_VERBS: tuple[str, ...] = ("播放", "放", "播", "来", "听", "看", "唱")
_PLAY_ALT = "|".join(_PLAY_VERBS)

# 动词在前（放点戏听听 / 我想听豫剧）或宾语前置（戏放一段）。
# 宾语前置只收「放/播/来」这类祈使动词，避免「这首歌真好听」被当成点播。
MEDIA_PLAY_PATTERN = (
    rf"(?:{_PLAY_ALT})[^。！？，,；;]{{0,4}}(?:{GENRE_ALT})"
    rf"|(?:{GENRE_ALT})[^。！？，,；;]{{0,4}}(?:播放|放|播|来)"
)
_MEDIA_PLAY_RE = re.compile(MEDIA_PLAY_PATTERN)

# 否定/延后播放：「别放越剧了」是停止，「先别放，等我吃完药」是延后。
_NEGATED_PLAY_RE = re.compile(
    r"(?:别|不要|不用|甭|不许|不想|不)(?:再)?(?:放|播|听(?!话|见|懂|到))(?:了|吧)?"
)
_DEFERRED_PLAY_RE = re.compile(
    r"(?:先|暂时)别(?:放|播)|等我[^。！？，,；;]{0,8}(?:再|以后|之后)"
)


def is_negated_play_command(text: str) -> bool:
    """否定播放指令（别放/不要播），语义是「别放」，不是「要放」。"""
    return bool(_NEGATED_PLAY_RE.search(text or ""))


def is_deferred_play_command(text: str) -> bool:
    """带条件的延后指令，需要等条件满足后再执行，不能立即停。"""
    return bool(_DEFERRED_PLAY_RE.search(text or ""))


def extract_genre(text: str) -> str | None:
    """从文本里提取媒体品类；提取不到返回 None，由 Tool 侧决定默认值。"""
    match = _GENRE_RE.search(text or "")
    return match.group(0) if match else None


def looks_media_play(text: str) -> bool:
    """是否在请求点播某个品类（放点戏听听 / 豫剧来一段）。"""
    if is_media_preference(text):
        return False
    return bool(_MEDIA_PLAY_RE.search(text or ""))


# 纯偏好表达（我爱听戏 / 不喜欢听戏）不是点播指令；带「放/播/来」才算请求。
_PREFERENCE_RE = re.compile(r"(?:喜欢|爱|讨厌)(?:听|看|唱)")
_IMPERATIVE_RE = re.compile(r"播放|放段|放点|放个|放首|放一|来段|来点|来个|来首|来一")


def is_media_preference(text: str) -> bool:
    normalized = text or ""
    if not _PREFERENCE_RE.search(normalized):
        return False
    return not _IMPERATIVE_RE.search(normalized)


# 评价/赞叹（这首越剧真好听 / 这戏唱得真好听）是在夸，不是点播。
# 与偏好表达分开：偏好只挡「喜欢/爱」，评价句常带品类词，会被分类器判成高置信点播。
_EVALUATION_RE = re.compile(
    r"(?:真|很|挺|太|蛮|特别|非常|好)(?:好听|好看|好唱|精彩)"
    r"|唱得(?:真|很|挺|太|可)?(?:好|不错)"
    r"|(?:真|太)棒了"
)


def is_media_evaluation(text: str) -> bool:
    """评价句不得触发点播；带明确祈使（放段/来一段）时不算评价。"""
    normalized = text or ""
    if not _EVALUATION_RE.search(normalized):
        return False
    return not _MEDIA_PLAY_RE.search(normalized) and not _IMPERATIVE_RE.search(normalized)


# 句末「然后呢/还有呢」是对上一轮的追问，不是新指令：
# 「听戏然后呢」问的是接下来做什么，不能当成一次新的点播。
_FOLLOWUP_RE = re.compile(r"(?:然后|接着|后来|还有)呢[。！？?…~～]*$")


def is_followup_question(text: str) -> bool:
    return bool(_FOLLOWUP_RE.search(text or ""))


# 否认句（我没说要听戏）不是点播：句中出现「听戏」也不能执行。
# 真正的停止指令（我不想听戏）在此之前由否定播放分支处理。
_DENIED_MEDIA_RE = re.compile(
    r"(?:没|不)(?:有)?(?:说|让|叫|要|想)(?:要|听|看|放|播)"
)


def is_denied_media_command(text: str) -> bool:
    return bool(_DENIED_MEDIA_RE.search(text or ""))


# ---------------- 播放控制 ----------------

_VOLUME_DOWN_RE = re.compile(
    r"(?:声音|音量|声儿|声)(?:调|弄|放|关|开|拧)?(?:小|低)"
    r"|(?:调|弄|放|关|开|拧)(?:小|低)(?:一?点|一点儿|些)?"
    r"|(?:小|低)(?:一?点|一点儿)(?:声|声音)?"
    r"|太(?:吵|响|大|大声)"
    r"|(?:声音|音量|声儿|声)(?:有点|有些|太|很|好)(?:大|吵|响)"
    r"|(?:声音|音量)(?:吵|响)"
)

_VOLUME_UP_RE = re.compile(
    r"(?:声音|音量|声儿|声)(?:调|弄|放|开|拧)?(?:大|高)"
    r"|(?:调|弄|放|开|拧)(?:大|高)(?:一?点|一点儿|些)?"
    r"|再(?:大|高)(?:一?点|一点儿|些)"
    r"|大声(?:一?点|点儿|些)?|大(?:一?点|点儿)?声"
    r"|听不清|听不见"
    r"|(?:声音|音量)(?:太|好|很|有点)(?:小|低)"
)

_NEXT_RE = re.compile(
    r"换台|换个|换一段|换一个|换一首|下一段|下一个|下一首|再来一段|再换一个"
)
_STOP_RE = re.compile(
    r"停(?:下|止|一下)?|先停一下|别说了|别放了|别播了|不要放了|"
    r"安静|关掉|关了吧|关一下|暂停|先暂停"
)


def detect_media_control(text: str) -> str | None:
    """把播放控制类自然语言映射到确定性动作；无法判断返回 None。"""
    if _VOLUME_DOWN_RE.search(text or ""):
        return "volume_down"
    if _VOLUME_UP_RE.search(text or ""):
        return "volume_up"
    if _NEXT_RE.search(text or ""):
        return "media_next"
    if _STOP_RE.search(text or ""):
        return "media_stop"
    return None


# ---------------- 用药 ----------------

MEDICATION_WORDS: tuple[str, ...] = (
    "吃药",
    "吃过药",
    "吃完药",
    "服药",
    "药盒",
    "药片",
    "降压药",
    "降糖药",
    "漏吃",
    "副作用",
    "药还",
    "药吃",
    "药没",
    "药什么时候",
)

# 「动词+宾语」两种语序都要认：把药吃了 / 药吃了 / 药还没服。
# 否定拒绝（我不吃药）由路由层先拦截，不依赖这里排除。
MEDICATION_PATTERN = (
    r"吃(?:过|完|了)?药"
    r"|服(?:过|完|了)?药"
    r"|药(?:还|又|都|已|已经|快|要|什么时[候辰]|啥时候)?(?:没|未)?(?:吃|服)"
    r"|药片(?:还|快|要)?(?:没|未)?(?:吃|服|完)"
    r"|该吃(?:药|降压药|降糖药)"
    r"|吃什么药|吃药了吗|提醒我吃药"
)
_MEDICATION_RE = re.compile(MEDICATION_PATTERN)


def looks_medication_query(text: str) -> bool:
    return bool(_MEDICATION_RE.search(text or ""))


# 服药回复（Day7 段3）：确认 / 否认 / 延后三分流。
# 「我不吃药」是拒绝用药，由 router/service 的 _NEGATED_MEDICATION_RE 先拦截，
# 不进入这里的词表；疑问句（吃了吗/吃药了吗）用负向前瞻排除。
_MEDICATION_DEFERRED_RE = re.compile(
    r"(?:等|过|待)(?:一)?会[儿]?(?:再)?吃|待会儿吃|晚点(?:儿)?吃|一会(?:儿)?(?:再)?吃"
)
_MEDICATION_DENIED_RE = re.compile(
    r"(?:还|都|又|今天|现在)?没(?:有)?(?:吃|服)(?:药)?|药(?:还|都|又)?没(?:吃|服)"
)
_MEDICATION_CONFIRMED_RE = re.compile(
    r"(?:已经|刚|刚才)?(?<!快)(?<!要)(?<!片)吃(?:过|完|了)(?:药)?(?!吗|么|饭|菜|没)"
    r"|服(?:过|完|了)药"
    r"|药(?:已经|已|刚)?吃(?:了|过|完)(?!吗|么|没)"
)
# 疑问句（吃过了吗 / 药吃了没）是在问，不是确认，必须先排除
_MEDICATION_QUESTION_RE = re.compile(
    r"(?:吗|么)\s*[。！？?]?$|吃(?:了|过)?没(?:有)?\s*[。！？?]?$"
)


def detect_medication_reply(text: str) -> str | None:
    """把服药回复分成 confirmed / denied / deferred；不是服药回复返回 None。

    顺序固定为延后 → 否认 → 确认：「等会儿吃」不是否认，「还没吃」不是确认。
    """
    normalized = text or ""
    if _MEDICATION_QUESTION_RE.search(normalized):
        return None
    if _MEDICATION_DEFERRED_RE.search(normalized):
        return "deferred"
    if _MEDICATION_DENIED_RE.search(normalized):
        return "denied"
    if _MEDICATION_CONFIRMED_RE.search(normalized):
        return "confirmed"
    return None


# ---------------- 时间 ----------------

TIME_WORDS: tuple[str, ...] = (
    "几点",
    "什么时间",
    "几号",
    "星期几",
    "时间了",
    "啥时候",
    "啥时辰",
    "什么时辰",
)

TIME_PATTERN = r"几点|什么时间|啥时候|啥时辰|什么时辰|几号|星期几|时间了"


def looks_time_query(text: str) -> bool:
    return bool(re.search(TIME_PATTERN, text or ""))


# ---------------- 多意图计数用词表 ----------------

CONTROL_WORDS: tuple[str, ...] = (
    "大声",
    "小声",
    "声音",
    "音量",
    "调大",
    "调小",
    "换台",
    "换一段",
    "换个",
    "下一段",
    "下一首",
    "别放",
    "停下",
    "停一下",
    "关小",
    "弄小",
    "太吵",
    "太响",
)

WEATHER_WORDS: tuple[str, ...] = (
    "天气",
    "下雨",
    "下雪",
    "降温",
    "冷不冷",
    "热不热",
    "风大",
)

HEALTH_WORDS: tuple[str, ...] = (
    "血压",
    "血糖",
    "心率",
    "血氧",
    "体重",
    "量一下",
    "测一下",
    "体检",
    "膝盖疼",
    "腰疼",
)
