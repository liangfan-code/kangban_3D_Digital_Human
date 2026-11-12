"""规则层（Day6.1）：10 条高频、稳定、无歧义的意图映射。

设计约束（大纲课时 6.1 / 7.3）：
- 只收确定性指令，做减法；超过 10 条后维护成本会失控；
- 命中即执行，零延迟、零 token，不调分类器也不调主控 Agent；
- 模式尽量锚定整句，避免「停车」「再见你」这类正常叙述被误命中；
- 品类/用药/控制的词表统一来自 ``app.router.signals``，不在这里另写一份；
- 安全词先于本层判定（见 ``app/router/service.py``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.router.schemas import Intent
from app.router.signals import (
    MEDIA_PLAY_PATTERN,
    MEDICATION_PATTERN,
    TIME_PATTERN,
    extract_genre,
    is_media_preference,
)


@dataclass(frozen=True)
class Rule:
    """一条规则：正则命中后给出意图、本地动作与可选 Tool 名。"""

    id: str
    name: str
    pattern: re.Pattern[str]
    intent: Intent
    action: str
    tool: str | None = None


@dataclass(frozen=True)
class RuleHit:
    rule: Rule
    genre: str | None = None


# 展示顺序即匹配顺序；正则之间尽量不重叠，重叠时以先声明者为准。
RULES: tuple[Rule, ...] = (
    Rule(
        "R01",
        "换一段",
        re.compile(
            r"^(换一段|换一个|换一首|下一段|下一首|换首|再来一段|再换一个|"
            r"换台|换个台|换个频道|换频道)$"
        ),
        Intent.MEDIA_CONTROL,
        "media_next",
        tool="media_next",
    ),
    Rule(
        "R02",
        "大声点",
        re.compile(
            r"^(?:声音|音量|声儿|声)(?:弄得?|调|放|开|拧)?(?:大|高)"
            r"(?:一?点|一点儿|点儿|些)?$"
            r"|^(?:把)?(?:声音|音量|声儿|声)?(?:弄得?|调|放|开|拧)"
            r"(?:大|高)(?:一?点|一点儿|点儿|些)?$"
            r"|^大声(?:一?点|点儿|些)?$|^大(?:一?点|点儿)?声$"
        ),
        Intent.MEDIA_CONTROL,
        "volume_up",
    ),
    Rule(
        "R03",
        "小声点",
        re.compile(
            r"^(?:声音|音量|声儿|声)(?:弄得?|关|调|放|开|拧)?(?:小|低)"
            r"(?:一?点|一点儿|点儿|些)?$"
            r"|^(?:把)?(?:声音|音量|声儿|声)?(?:弄得?|关|调|放|开|拧)"
            r"(?:小|低)(?:一?点|一点儿|点儿|些)?$"
            r"|^小声(?:一?点|一点儿|点儿)?$|^小(?:一?点|点儿)?声$"
            r"|^声音(?:太(?:吵|响|大)|(?:吵|响))$"
        ),
        Intent.MEDIA_CONTROL,
        "volume_down",
    ),
    Rule(
        "R04",
        "停",
        re.compile(
            r"^(停|停下|停止|停一下|先停一下|别说了|别放了|别播了|不要放了|"
            r"不要说了|安静|关掉|关了吧|关一下|暂停|暂停一下|先暂停)$"
        ),
        Intent.MEDIA_CONTROL,
        "media_stop",
        tool="media_stop",
    ),
    Rule(
        "R05",
        "戏曲/电影点播",
        re.compile(MEDIA_PLAY_PATTERN),
        Intent.MEDIA_PLAY,
        "media_play",
        tool="media_play",
    ),
    Rule(
        "R06",
        "几点了",
        re.compile(
            rf"^(?:顺便问一下|顺便问下|问一下)?(?:今天|现在)?"
            rf"(?:{TIME_PATTERN})(?:了|呢)?$"
        ),
        Intent.OTHER,
        "time_query",
    ),
    Rule(
        "R07",
        "天气",
        re.compile(
            r"(今天|明天|外面).{0,4}天气|天气(怎么样|如何|咋样)|"
            r"(今天|明天).{0,4}(下雨|冷不冷|热不热)"
        ),
        Intent.WEATHER,
        "weather_query",
        tool="weather_get",
    ),
    Rule(
        "R08",
        "我要吃药",
        re.compile(MEDICATION_PATTERN),
        Intent.MEDICATION_QUERY,
        "medication_query",
        tool="medication_query",
    ),
    Rule(
        "R09",
        "再见",
        re.compile(r"(再见|拜拜|下次再聊|我走了|先这样吧|回头见)"),
        Intent.CHITCHAT,
        "farewell",
    ),
    Rule(
        "R10",
        "重复一遍",
        re.compile(
            r"(重复一遍|再说一遍|"
            r"再(?:给)?我?(?:说|讲|告诉)[^。！？，,]{0,2}(?:一遍|一次)|"
            r"没听清.{0,4}再说|刚才说什么|你刚才说什么)"
        ),
        Intent.OTHER,
        "repeat",
    ),
)

_PUNCTUATION = re.compile(r"[\s。！？!?，,、；;：:…~～\"'“”‘’]+")


def normalize_text(text: str) -> str:
    """去掉空白与句末标点，便于整句锚定的规则匹配（ASR 常带标点）。"""
    return _PUNCTUATION.sub("", text or "")


def match_rule(text: str) -> RuleHit | None:
    """按声明顺序返回首个命中规则；未命中返回 None。"""
    normalized = normalize_text(text)
    if not normalized:
        return None
    for rule in RULES:
        # 纯偏好表达（我爱听戏）不是点播指令，不能因为含品类词就放戏
        if rule.id == "R05" and is_media_preference(normalized):
            continue
        if rule.pattern.search(normalized) is not None:
            return RuleHit(rule=rule, genre=extract_genre(normalized))
    return None
