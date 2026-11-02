"""原生观察 Hook：只补充数据，不接管正文投递或 Gateway 控制流."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_logger = logging.getLogger(__name__)


def _dispatch(kind: str, payload: dict[str, Any]) -> None:
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home

    from .adapter import ACTIVE
    from .plugin import PLATFORM

    if get_session_env("HERMES_SESSION_PLATFORM") != PLATFORM:
        return
    # 入站 ID 由官方 contextvars 随观察事件传递，不使用模型 turn_id 匹配 Gateway UUID
    source_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
    if not source_id:
        return
    home = get_hermes_home().resolve()
    for adapter in list(ACTIVE):
        if adapter.cards._cfg._home != home:
            continue
        loop = adapter._card_loop
        if loop is None or loop.is_closed():
            continue

        def schedule(target: Any = adapter) -> None:
            task = asyncio.create_task(target.cards.observe(source_id, kind, payload))
            task.add_done_callback(_report)

        loop.call_soon_threadsafe(schedule)


def _report(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        _logger.error("Card observer failed", exc_info=task.exception())


def observe_start(**kwargs: Any) -> None:
    _dispatch("start", kwargs)


def observe_reasoning(**kwargs: Any) -> None:
    if kwargs.get("kind") == "reasoning":
        _dispatch("reasoning", kwargs)


def observe_tool_start(**kwargs: Any) -> None:
    _dispatch("tool_start", kwargs)


def observe_tool_end(**kwargs: Any) -> None:
    _dispatch("tool_end", kwargs)


def observe_usage(**kwargs: Any) -> None:
    _dispatch("usage", kwargs)


def observe_end(**kwargs: Any) -> None:
    _dispatch("end", kwargs)
