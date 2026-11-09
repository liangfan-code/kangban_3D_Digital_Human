"""规则层（Day6.1）：10 条高频、稳定、无歧义的意图映射。

设计约束（大纲课时 6.1 / 7.3）：
- 只收确定性指令，做减法；超过 10 条后维护成本会失控；
- 命中即执行，零延迟、零 token，不调分类器也不调主控 Agent；
- 模式尽量锚定整句，避免「停车」「我吃了药」这类正常叙述被误命中；
- 安全词先于本层判定（见 ``app/router/service.py``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.router.schemas import Intent


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
        re.compile(r"^(换一段|换一个|换一首|下一段|下一首|换首|再来一段|再换一个)$"),
        Intent.MEDIA_CONTROL,
        "media_next",
    ),
    Rule(
        "R02",
        "大声点",
        re.compile(r"^(大声点|大点声|大声一点|声音大点|声音大一点)$"),
        Intent.MEDIA_CONTROL,
        "volume_up",
    ),
    Rule(
        "R03",
        "小声点",
        re.compile(r"^(小声点|小点声|小声一点|声音小点|声音小一点)$"),
        Intent.MEDIA_CONTROL,
        "volume_down",
    ),
    Rule(
        "R04",
        "停",
        re.compile(r"^(停|停下|停止|别说了|别放了|先停一下|不要说了|安静)$"),
        Intent.MEDIA_CONTROL,
        "media_stop",
    ),
    Rule(
        "R05",
        "戏曲/电影点播",
        re.compile(
            r"(我想听|想听|放一段|放一首|放首|来一段|来一首|来首|播放).{0,6}"
            r"(?P<genre>越剧|京剧|黄梅戏|沪剧|评弹|戏曲|老歌|老电影|电影)"
        ),
        Intent.MEDIA_PLAY,
        "media_play",
    ),
    Rule(
        "R06",
        "几点了",
        re.compile(r"^(几点了|现在几点|现在几点了|什么时间了|现在什么时间)$"),
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
        re.compile(r"(我要|我该|我得|准备|到点).{0,3}吃药|吃什么药|吃药了吗|提醒我吃药|该吃药"),
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
        re.compile(r"(重复一遍|再说一遍|没听清.{0,4}再说|刚才说什么|你刚才说什么)"),
        Intent.OTHER,
        "repeat",
    ),
)

_PUNCTUATION = re.compile(r"[\s。！？!?，,、；;：:…~～\"'“”‘’]+")
_GENRE_PATTERN = re.compile(r"(越剧|京剧|黄梅戏|沪剧|评弹|戏曲|老歌|老电影|电影)")


def normalize_text(text: str) -> str:
    """去掉空白与句末标点，便于整句锚定的规则匹配（ASR 常带标点）。"""
    return _PUNCTUATION.sub("", text or "")


def extract_genre(text: str) -> str | None:
    """从文本里提取媒体品类；提取不到返回 None，由 Tool 侧决定默认值。"""
    match = _GENRE_PATTERN.search(normalize_text(text))
    return match.group(1) if match else None


def match_rule(text: str) -> RuleHit | None:
    """按声明顺序返回首个命中规则；未命中返回 None。"""
    normalized = normalize_text(text)
    if not normalized:
        return None
    for rule in RULES:
        match = rule.pattern.search(normalized)
        if match is not None:
            return RuleHit(rule=rule, genre=(match.groupdict().get("genre") or None))
    return None
