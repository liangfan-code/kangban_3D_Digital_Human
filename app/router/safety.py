"""安全一票否决（Day6）：先于规则层与任何模型。

原则（大纲课时 6.3）：
- 命中即进待人工确认队列（Day6.3 接入），只播预设安抚话术；
- 宁可错杀，不做医疗判断，不说「您这是××病」；
- 纯本地词表 + 组合症状模式，零延迟、不依赖 LLM/分类器是否可用，
  模型层故障时依然生效。

词表之外还有一类「词表覆盖不到」的高危表达（如「我的心脏病犯了」）：
这类句子没有固定关键词，但由「慢病名 + 发作动词」或「部位 + 症状」组合而成，
因此用本地正则模式兜底，避免只有命中固定词才建工单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.router.rules import normalize_text

# 分类 → 关键词。只收明确的高危表达，避免把「天气难受」之类日常句误判；
# 业务侧仍按「宁可错杀」处理：命中即人工兜底。
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "急症",
        (
            "胸口疼",
            "胸口痛",
            "胸痛",
            "胸口闷",
            "胸闷",
            "胸口发紧",
            "心口疼",
            "心口堵",
            "心脏难受",
            "喘不上气",
            "喘不过气",
            "喘不过来气",
            "喘不上来气",
            "呼吸困难",
            "气短",
            "心悸",
            "中风",
            "半身麻",
            "说话不利索",
            "说话不清",
            "嘴歪",
            "晕倒",
            "昏过去",
            "吐血",
            "眼前发黑",
        ),
    ),
    (
        "跌倒",
        (
            "摔了",
            "摔倒",
            "跌倒",
            "滑倒",
            "摔跤",
            "起不来",
            "爬不起来",
            "从床上掉",
            "没站稳",
            "腿一软",
            "摔下去",
            "跌了一跤",
        ),
    ),
    (
        "走失",
        (
            "找不到家",
            "找不着家",
            "迷路",
            "走丢",
            "不知道这是哪",
            "回不去",
            "找不到房间",
            "找不到回去的路",
        ),
    ),
    (
        "情绪低落",
        (
            "不想活",
            "想死",
            "活着没意思",
            "没人管我",
            "不如死了",
            "想不开",
            "轻生",
        ),
    ),
    (
        "身体不适",
        (
            "不舒服",
            "难受",
            "头晕",
            "恶心想吐",
            "疼得厉害",
            "浑身没劲",
            "心慌",
            "发烧",
            "发热",
            "拉肚子",
        ),
    ),
)

# 组合症状模式 → (类别, 模式名)。关键词表只能覆盖固定说法，
# 这里补「慢病名 + 发作动词」「部位 + 症状」「突发 + 急性表现」等常见变体；
# 模式名写日志，避免把对话原文带进日志。
_PATTERN_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "急症",
        "急症-慢病急性发作",
        re.compile(
            r"(心脏病|冠心病|心绞痛|心肌梗死|心梗|脑梗|脑中风|中风|癫痫|哮喘|老毛病)"
            r"[^。！？]{0,3}(犯|发作|发了|又犯|来了|起来)"
        ),
    ),
    (
        "急症",
        "急症-慢病发作句式",
        re.compile(
            r"(犯|发作|发了)[^。！？]{0,2}"
            r"(心脏病|冠心病|心绞痛|心梗|脑梗|中风|癫痫|哮喘|老毛病)"
        ),
    ),
    (
        "急症",
        "急症-心胸不适",
        re.compile(
            r"(心脏|心口|胸口|胸前|胸)[^。！？]{0,4}"
            r"(疼|痛|闷|堵|紧|压|针扎|揪|难受|不舒服|发慌|心慌)"
        ),
    ),
    (
        "急症",
        "急症-剧痛",
        re.compile(
            r"(疼|痛|难受|不舒服)(得|的)?(厉害|要命|受不了|不行|直冒汗|打滚|直不起腰)"
        ),
    ),
    (
        "急症",
        "急症-突发",
        re.compile(
            r"(突然|忽然|一下子|猛地)[^。！？]{0,6}"
            r"(疼|痛|晕|麻|吐|喘|看不清|站不住|站不稳|说不出话|没力气|摔倒)"
        ),
    ),
    (
        "急症",
        "急症-呼吸困难",
        re.compile(r"(喘不上|喘不过|上不来气|呼吸(困难|急促|费劲)|憋气|憋得慌)"),
    ),
    (
        "急症",
        "急症-肢体异常",
        re.compile(
            r"(手脚|半边身子|半身|一侧|胳膊|腿)[^。！？]{0,4}"
            r"(发麻|麻了|麻|发木|木了|无力|没知觉|不听使唤|动不了)"
        ),
    ),
    (
        "急症",
        "急症-意识异常",
        re.compile(r"(昏迷|休克|抽搐|抽风|口吐白沫|翻白眼|叫不醒|没意识|意识不清)"),
    ),
    (
        "急症",
        "急症-出血",
        re.compile(r"(吐血|咳血|便血|尿血|流血不止|出血不止|血止不住)"),
    ),
    (
        "急症",
        "急症-危急表达",
        re.compile(r"我[^。！？]{0,6}(快不行了|撑不住了|受不了了|坚持不住了)"),
    ),
    # 以下为口语变体：真实长者很少说「站不稳」，更多是「站不住了」「腿发软」；
    # 关键词表按连续子串匹配会漏掉夹字表述，这里统一用正则兜底。
    (
        "跌倒",
        "跌倒-站立不稳",
        re.compile(r"(站不稳|站不住|站不住了|腿发软|两腿发软|两条腿发软|腿软|腿一软)"),
    ),
    (
        "急症",
        "急症-言语不清",
        re.compile(r"说话[^。！？，,]{0,4}(不清|不清楚|不利索|含糊|含混|大舌头)"),
    ),
    (
        "急症",
        "急症-视力异常",
        re.compile(r"眼前[^。！？，,]{0,3}(一黑|发黑|发花|看不清|看不见|冒金星)"),
    ),
    (
        "急症",
        "急症-口角歪斜",
        re.compile(r"嘴[^。！？，,]{0,2}歪"),
    ),
    (
        "急症",
        "急症-冷汗虚脱",
        re.compile(r"(冒|出|一身)冷汗|虚脱"),
    ),
    (
        "急症",
        "急症-持物不稳",
        re.compile(r"(拿不住|握不住|端不住|拿不稳)[^。！？，,]{0,4}(杯子|水杯|碗|筷子|勺子|东西)?"),
    ),
    (
        "身体不适",
        "身体不适-轻度不适",
        re.compile(r"不(?:太|怎么|大)?舒服|不(?:太|怎么)?得劲"),
    ),
    (
        "身体不适",
        "身体不适-头晕变体",
        re.compile(r"(头|脑袋)[^。！？，,]{0,3}(晕|发昏|昏沉)"),
    ),
)

# 否定前缀：长者常说「我没有不舒服」「我不头晕」，字面命中症状词但语义是否认。
# 只抑制「否认症状」的表述，不影响「我不舒服」「疼得厉害」等真实症状。
_NEGATION_RE = re.compile(
    r"(?:没有|没|并不|不怎么|不觉得|没觉得|不是|不)"
    r"[^。！？，,]{0,3}"
    r"(?:不(?:太|怎么|大)?舒服|难受|头晕|心慌|发烧|发热|拉肚子|疼|痛|胸闷|麻|喘不上气)"
)

# 症状词内部夹否定：「胸口不疼」「腿不麻」「头不晕」。
_INNER_NEGATION_RE = re.compile(
    r"(?:胸口|心口|胸|心脏|头|腿|手|脚|半身|半边身子)"
    r"[^。！？，,]{0,3}(?:不|没)[^。！？，,]{0,3}"
    r"(?:疼|痛|闷|堵|晕|麻|难受|舒服)"
)


def _negated_spans(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _NEGATION_RE.finditer(text)]
    spans.extend((m.start(), m.end()) for m in _INNER_NEGATION_RE.finditer(text))
    return spans


def _inside(start: int, spans: list[tuple[int, int]]) -> bool:
    return any(left <= start < right for left, right in spans)


@dataclass(frozen=True)
class SensitiveHit:
    """命中的敏感类别与关键词（关键词只用于日志排查，不写原文）。"""

    category: str
    keyword: str


def match_sensitive(text: str) -> SensitiveHit | None:
    """返回首个命中的敏感类别；未命中返回 None。

    否定表述（「我没有不舒服」「胸口不疼」）不计为症状，避免误建工单；
    同一句若另有未被否定的症状（「没有不舒服，但是胸口疼」）仍会命中。
    """
    if not text:
        return None
    normalized = normalize_text(text)
    negated = _negated_spans(normalized)
    for category, keywords in _PATTERNS:
        for keyword in keywords:
            start = normalized.find(keyword)
            while start != -1:
                if not _inside(start, negated):
                    return SensitiveHit(category=category, keyword=keyword)
                start = normalized.find(keyword, start + 1)
    for category, label, pattern in _PATTERN_RULES:
        for match in pattern.finditer(normalized):
            if not _inside(match.start(), negated):
                return SensitiveHit(category=category, keyword=label)
    return None
