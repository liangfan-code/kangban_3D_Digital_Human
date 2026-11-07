"""动作标记协议（整句版）：动作表加载、一次性标记解析与规则触发表。

协议约定与老师参考实现（asr/src/asr/actions.py 流式版）保持一致：
标记形状为 ``[act:<action_id>]`` 或 ``[act:<action_id>:<repeat>]``；
标记必须在文本进入字幕 / TTS 之前剥除，动作经 WS 下发给前端。

本项目回复为非流式整句（Day4 无 LLM），故无需流式 ActionMarkerFilter，
用 ``parse_action_markers`` 一次性解析即可；Day5 接入 LLM 后仅替换触发源，
本模块协议形状与硬规则不变。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ACTIONS_PATH = Path(__file__).resolve().parents[1] / "static" / "actions.json"

# 完整标记：[act:wave] / [act:nod:2]（与老师协议同款正则，忽略大小写）
_MARKER_RE = re.compile(
    r"\[(?:act\s*[:：]\s*([a-z0-9_-]+)(?:\s*[:：]\s*([a-z0-9_-]+))?|([a-z0-9_-]{2,24}))\]",
    re.IGNORECASE,
)

_REPEAT_RANGE = (1, 3)


@dataclass(frozen=True)
class ActionDef:
    """动作表中的一个动作（Day5 生成 LLM 提示词时需 label / usage）。"""

    id: str
    label: str = ""
    usage: str = ""


@dataclass(frozen=True)
class ActionCatalog:
    """动作表子集：本项目只需允许列表与硬规则参数。"""

    actions: tuple[ActionDef, ...] = ()
    marker: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    @property
    def allowed_ids(self) -> frozenset[str]:
        return frozenset(action.id for action in self.actions)


@lru_cache(maxsize=8)
def _load_catalog_cached(path: str, mtime_ns: int) -> ActionCatalog:
    del mtime_ns
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = tuple(
        ActionDef(
            id=str(item["id"]).lower(),
            label=str(item.get("label") or item["id"]),
            usage=str(item.get("usage") or ""),
        )
        for item in data.get("actions") or []
        if isinstance(item, dict) and item.get("id")
    )
    return ActionCatalog(
        actions=actions,
        marker=dict(data.get("marker") or {}),
        source=Path(path),
    )


def load_catalog(path: str | Path | None = None) -> ActionCatalog:
    """读取动作表；文件变更后按 mtime 自动重新加载。"""

    target = Path(path) if path is not None else DEFAULT_ACTIONS_PATH
    return _load_catalog_cached(str(target), target.stat().st_mtime_ns)


def get_catalog(path: str | Path | None = None) -> ActionCatalog | None:
    """容错读取：动作表缺失或损坏时不阻断语音链路，只是本次不下发动作。"""

    try:
        return load_catalog(path)
    except Exception:
        logger.exception("动作表加载失败，本次回复不解析动作")
        return None


def parse_action_markers(
    text: str, catalog: ActionCatalog | None = None
) -> tuple[str, list[tuple[str, str | None]]]:
    """整句一次性解析：返回 (剥净标记的正文, 合法动作列表)。

    硬规则与老师协议一致（取自动作表 marker 配置，缺省 2 / True）：
    - 每次回复最多下发 ``max_per_reply`` 个动作；
    - 禁止连续下发同一动作；
    - 未知动作 id 静默丢弃（但标记照样剥除，绝不进入正文）；
    - repeat 参数非 1-3 时降级为播放一次（param=None）。
    动作表不可用时（catalog 为 None）仍剥除全部标记，只是不下发动作。
    """

    cat = catalog if catalog is not None else get_catalog()
    allowed = cat.allowed_ids if cat else frozenset()
    rules = cat.marker if cat else {}
    max_per_reply = int(rules.get("max_per_reply") or 2)
    no_repeat_consecutive = bool(rules.get("no_repeat_consecutive", True))

    actions: list[tuple[str, str | None]] = []
    last_action: str | None = None

    def _take(match: re.Match[str]) -> str:
        nonlocal last_action
        # group(1)=act: 完整格式的 id；group(3)=LLM 偶发的裸简写 [wave]（实测出现
        # [tilt]/[act: nod:1] 两种走样写法，正则容错 + 这里统一归一）
        action_id = ((match.group(1) or match.group(3)) or "").lower()
        param = (match.group(2) or "").strip() or None

        if not allowed or action_id not in allowed:
            logger.warning("丢弃未知动作标记 action=%s", action_id)
            return ""
        if len(actions) >= max_per_reply:
            logger.info("丢弃超出上限的动作标记 action=%s", action_id)
            return ""
        if no_repeat_consecutive and action_id == last_action:
            logger.info("丢弃连续重复的动作标记 action=%s", action_id)
            return ""

        if param is not None:
            try:
                if not _REPEAT_RANGE[0] <= int(param) <= _REPEAT_RANGE[1]:
                    param = None
            except ValueError:
                param = None

        actions.append((action_id, param))
        last_action = action_id
        return ""

    cleaned = _MARKER_RE.sub(_take, text)
    return cleaned, actions


# 无 LLM 阶段的动作触发源（Day4 教学演示）：关键词命中即在回复句首补标记。
# Day5 接入 LLM 后由 build_action_prompt 协议注入替代，本表随即废弃。
# 十组语义场景，一组可含多个等价关键词；动作 id 必须来自 actions.json。
_REPLY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("你好", "您好", "hello"), "wave"),
    (("再见", "拜拜", "bye"), "bye"),
    (("谢谢", "感谢"), "nod"),
    (("好的", "嗯", "是的"), "nod"),
    (("不要", "不行", "不可以"), "shake"),
    (("让我想想", "想一下"), "think"),
    (("什么", "没听清"), "tilt"),
    (("不舒服", "难受", "疼"), "comfort"),
    (("恭喜", "生日快乐"), "clap"),
    (("活动一下", "锻炼"), "stretch"),
)


def apply_reply_rules(text: str) -> str:
    """按规则触发表在回复句首补动作标记（最多 max_per_reply 个、禁连续同动作）。

    只在「用户终句 → 回复」这一步调用；产出的标记随后经
    ``parse_action_markers`` 统一解析，保证管线单一出口。
    """

    if not text or not text.strip():
        return text
    lowered = text.lower()
    hits: list[str] = []
    for keywords, action_id in _REPLY_RULES:
        if any(keyword in lowered for keyword in keywords):
            if hits and hits[-1] == action_id:
                continue
            hits.append(action_id)
            if len(hits) >= 2:
                break
    if not hits:
        return text
    return "".join(f"[act:{action_id}]" for action_id in hits) + text


def build_action_prompt(catalog: ActionCatalog | None = None) -> str:
    """生成动作协议说明，由回复链拼进 system prompt 供 LLM 注入标记。

    动作表不可用时返回空串（LLM 只产出纯文本，协议静默关闭，不报错）。
    """

    cat = catalog if catalog is not None else get_catalog()
    if cat is None or not cat.actions:
        return ""
    rules = cat.marker
    max_per_reply = int(rules.get("max_per_reply") or 2)
    repeat_rule = (
        "标记可带重复次数 [act:动作id:1~3]"
        if rules.get("repeat_allowed", True)
        else "标记不带重复次数"
    )
    lines = [
        "## 动作标记协议（严格遵守）",
        "- 需要数字人做动作时，在对应句子开头插入动作标记，标记本身不会朗读；",
        "- 标记格式必须严格为 [act:动作id]：半角方括号+半角冒号，冒号前后不能有空格，"
        "act: 前缀不能省略（写成 [动作id] 或 [act: 动作id] 都算错）；",
        f"- 每条回复最多 {max_per_reply} 个标记，禁止连续两个相同动作；{repeat_rule}；",
        "- 只能使用下列动作，禁止自造：",
    ]
    lines += [
        f"- {action.id}：{action.label}{('（' + action.usage + '）') if action.usage else ''}"
        for action in cat.actions
    ]
    lines.append(
        "日常寒暄与回答优先不加动作；仅在语义明显匹配（打招呼、告别、肯定、"
        "否定、思考、安抚等）时使用。"
    )
    return "\n".join(lines)
