"""HITL 待人工确认队列（Day6.3）：进程内存实现，Day8 建工单表时再落库。

边界：
- 只存"待人确认"的事件与必要上下文，不落原始音频；
- 敏感事件只把类别写进结构化日志，正文仅留在内存供管理员查看；
- 没有人工确认前，任何高危副作用（通知家属、建工单）都不得执行。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

_MAX_ITEMS = 200


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class HitlItem:
    id: str
    kind: str
    summary: str
    elder_id: str | None = None
    device_id: str = ""
    detail: str = ""
    created_at: str = field(default_factory=_now)
    status: str = "pending"
    resolved_by: str | None = None
    resolved_at: str | None = None


class HitlQueue:
    """线程安全的小容量队列；同一条待确认事件重复出现时复用原记录。"""

    def __init__(self, max_items: int = _MAX_ITEMS) -> None:
        self._items: dict[str, HitlItem] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_items = max_items

    def enqueue(
        self,
        *,
        kind: str,
        summary: str,
        elder_id: str | None = None,
        device_id: str = "",
        detail: str = "",
    ) -> HitlItem:
        with self._lock:
            for item_id in self._order:
                item = self._items[item_id]
                if (
                    item.status == "pending"
                    and item.kind == kind
                    and item.elder_id == elder_id
                    and item.detail == detail
                ):
                    return item
            item = HitlItem(
                id=uuid.uuid4().hex,
                kind=kind,
                summary=summary,
                elder_id=elder_id,
                device_id=device_id,
                detail=detail,
            )
            self._items[item.id] = item
            self._order.append(item.id)
            while len(self._order) > self._max_items:
                dropped = self._order.pop(0)
                self._items.pop(dropped, None)
            return item

    def pending(self) -> list[HitlItem]:
        with self._lock:
            return [self._items[i] for i in self._order if self._items[i].status == "pending"]

    def resolve(self, item_id: str, operator: str, note: str = "") -> HitlItem | None:
        with self._lock:
            item = self._items.get(item_id)
            if item is None or item.status != "pending":
                return None
            item.status = "resolved"
            item.resolved_by = operator
            item.resolved_at = _now()
            if note:
                item.detail = f"{item.detail}\n[处理备注] {note}".strip()
            return item

    def clear(self) -> None:
        """测试用：清空队列。"""
        with self._lock:
            self._items.clear()
            self._order.clear()


hitl_queue = HitlQueue()
