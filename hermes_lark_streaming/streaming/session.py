"""Card session state shared by controller and card orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future as ConcurrentFuture
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Any

from .flush import CARDKIT_MS, FlushController
from .segments import Segment, SegmentState
from .tooluse import ToolUseTracker
from .unavailable_guard import UnavailableGuard

if TYPE_CHECKING:
    from .image import ImageResolver


class SessionState(StrEnum):
    IDLE = "idle"
    CREATING = "creating"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.ABORTED,
        }


class CardSession:
    """单条消息的卡片会话状态."""

    __slots__ = (
        "_loop",
        "anchor_id",
        "card_id",
        "card_msg_id",
        "chat_id",
        "create_task",
        "created_at",
        "deferred_background_review_closed",
        "deferred_background_review_lock",
        "deferred_background_reviews",
        "element_count",
        "flush",
        "footer",
        "guard",
        "image_resolver",
        "message_id",
        "segment_state",
        "sequence",
        "split_disabled",
        "split_index",
        "state",
        "tool_use",
    )

    def __init__(
        self,
        message_id: str,
        chat_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.message_id = message_id
        self.anchor_id: str | None = None
        self.chat_id = chat_id
        self.create_task: asyncio.Future[Any] | ConcurrentFuture | None = None
        self.state = SessionState.IDLE
        self.card_msg_id: str | None = None
        self.card_id: str | None = None
        self.tool_use = ToolUseTracker()
        self.flush = FlushController(throttle_ms=CARDKIT_MS)
        self.footer: dict[str, Any] = {}
        self.sequence = 1
        self._loop = loop
        self.created_at = time.time()
        self.deferred_background_review_closed = False
        self.deferred_background_reviews: list[tuple[str, Callable[[str], Any]]] = []
        self.deferred_background_review_lock = Lock()

        self.guard = UnavailableGuard(
            reply_to_message_id=message_id,
            get_card_message_id=lambda: self.card_msg_id,
            on_terminate=self.mark_failed,
        )

        self.image_resolver: ImageResolver | None = None
        self.segment_state: SegmentState | None = SegmentState()
        self.element_count: int = 0
        self.split_disabled = False
        self.split_index: int = 0

    @property
    def has_card(self) -> bool:
        return bool(self.card_id or self.card_msg_id)

    def set_card(self, *, card_id: str, card_msg_id: str) -> None:
        self.card_id = card_id
        self.card_msg_id = card_msg_id

    def mark_failed(self) -> None:
        self.state = SessionState.FAILED

    def active_segments(self) -> list[Segment]:
        if self.segment_state is None:
            return []
        return self.segment_state.segments[self.split_index:]
