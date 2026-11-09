"""System One 问题定义（Day6.2）。

五组问题固定下来，分类器 Provider（本地确定性实现或 Laya）只负责给出答案，
不各自发明标签，避免模型层的输出口径漂移：

1. ``intent``             —— 意图分类（可多选标签里的单选）
2. ``is_sensitive``       —— 是否属于敏感/高危表达（安全网的模型侧补充）
3. ``confidence``         —— 意图置信度（由选择概率换算，≥0.85 才允许单步执行）
4. ``is_private``         —— 双人间等公共场景下的隐私内容（不宜外放）
5. ``is_out_of_domain``   —— 域外问题（先澄清，不直接接主控 Agent）

约束：置信度阈值、超时、Provider 均来自 ``Settings``，不在代码里写死。
"""

from __future__ import annotations

from app.core.config import get_settings
from app.router.schemas import Intent

# 意图标签人话描述，既给 Laya criteria 用，也作为本地实现的可读注解
INTENT_CRITERIA: dict[str, str] = {
    Intent.CHITCHAT.value: "问候、寒暄、回忆、想家、家人、孤单等情感陪伴",
    Intent.MEDICATION_QUERY.value: "询问吃药、药盒、漏服、副作用、还剩几颗",
    Intent.MEDIA_PLAY.value: "想听戏曲、老歌、电影、评弹等娱乐点播",
    Intent.MEDIA_CONTROL.value: "换一段、音量大小、停止播放等播放控制",
    Intent.WEATHER.value: "今天/明天天气、下雨、降温、冷热、适不适合出门",
    Intent.HEALTH_COLLECT.value: "血压、血糖、心率、血氧、体重等健康数据采集",
    Intent.OTHER.value: "无法归入以上类别",
}

# Laya 的 choice 问题：一次性拿到意图 + 概率
INTENT_QUESTION: dict[str, dict] = {
    "intent": {
        "type": "choice",
        "instructions": "这句话最接近哪一类意图？",
        "criteria": dict(INTENT_CRITERIA),
    }
}

SENSITIVE_QUESTION: dict[str, dict] = {
    "is_sensitive": {
        "type": "noul",
        "instructions": "这句话是否涉及急症、跌倒、走失或情绪危机？",
    }
}

PRIVACY_QUESTION: dict[str, dict] = {
    "is_private": {
        "type": "noul",
        "instructions": "这句话是否包含密码、证件、银行卡、存款等不宜外放的隐私信息？",
    }
}

OUT_OF_DOMAIN_QUESTION: dict[str, dict] = {
    "is_out_of_domain": {
        "type": "noul",
        "instructions": "这句话是否超出养老陪伴场景（如股票、编程、法律诉讼）？",
    }
}

# 供 spike/验收文档核对"五组问题"是否齐全
QUESTIONS_SPEC: tuple[tuple[str, str], ...] = (
    ("intent", "意图分类"),
    ("is_sensitive", "敏感/高危"),
    ("confidence", "置信度闸门"),
    ("is_private", "双人间隐私"),
    ("is_out_of_domain", "域外检测"),
)

# 低置信 / 域外时的澄清话术：给两个最可能的方向，不猜着执行
CLARIFY_QUESTION = "您是想聊聊天，还是想听段戏或者查查天气？"

# 双人间等公共场景下的隐私词：命中只做标记，不在此处改变回复内容
PRIVACY_KEYWORDS: tuple[str, ...] = (
    "密码",
    "身份证",
    "银行卡",
    "存折",
    "存款",
    "工资",
    "退休金",
    "手机号",
    "电话号",
    "房产证",
    "钥匙放",
)


def confidence_threshold() -> float:
    """当前配置的单步执行置信度闸门。"""
    return get_settings().system_one_confidence_threshold
