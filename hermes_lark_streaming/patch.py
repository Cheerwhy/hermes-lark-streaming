"""AST Patch 注入的 Hook 函数.

这些函数从 gateway/run.py 的注入点被调用.
它们只做一件事：检查配置 → 调用 controller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from .controller import get_controller

_logger = logging.getLogger("hermes_lark_streaming")


def _safe_hook(
    default_return: Any = None,
    log_level: str = "warning",
) -> Callable:
    """统一处理 enabled 检查和异常捕获."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*, message_id: str, **kwargs: Any) -> Any:
            try:
                ctrl = get_controller()
                if not ctrl.enabled:
                    return default_return
                return func(ctrl=ctrl, message_id=message_id, **kwargs)
            except Exception as exc:
                getattr(_logger, log_level)("%s error: %s", func.__name__, exc, exc_info=True)
                return default_return

        return wrapper

    return decorator


def on_feishu_normalize(
    *,
    message_id: str,
    source: Any,
    event: Any,
    reply_anchor_id: str | None = None,
) -> None:
    """[注入点 0] _handle_message source 赋值后 — 修正飞书引用消息的虚假 thread_id."""
    ctrl = get_controller()
    if not ctrl.enabled:
        return

    platform = getattr(getattr(source, "platform", None), "value", "")
    if platform != "feishu":
        return

    raw = getattr(event, "raw_message", None)
    raw_event = raw.get("event") if isinstance(raw, dict) else None
    if raw_event is None:
        raw_event = getattr(raw, "event", None)

    raw_message = None
    if isinstance(raw_event, dict):
        raw_message = raw_event.get("message")
    elif raw_event is not None:
        raw_message = getattr(raw_event, "message", None)
    if raw_message is None and isinstance(raw, dict):
        raw_message = raw.get("message")
    if raw_message is None:
        raw_message = raw

    real_thread_id = None
    if isinstance(raw_message, dict):
        real_thread_id = raw_message.get("thread_id")
    else:
        real_thread_id = getattr(raw_message, "thread_id", None)

    reply_to = getattr(event, "reply_to_message_id", None)
    source_thread_id = getattr(source, "thread_id", None)

    _logger.info(
        "feishu inbound ids: msg=%s anchor=%s source_thread=%s raw_thread=%s reply_to=%s",
        message_id,
        reply_anchor_id,
        source_thread_id,
        real_thread_id,
        reply_to,
    )

    if reply_to and source_thread_id and not real_thread_id:
        source.thread_id = None
        event.source = source


@_safe_hook()
def on_message_started(*, ctrl: Any, message_id: str, chat_id: str, anchor_id: str | None = None) -> None:
    """[注入点 1] 函数开头 — message.started."""
    ctrl.on_message_started(message_id=message_id, chat_id=chat_id, anchor_id=anchor_id)


@_safe_hook(default_return=False)
def on_message_completed(
    *,
    ctrl: Any,
    message_id: str,
    answer: str = "",
    duration: float = 0.0,
    model: str = "",
    tokens: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    """[注入点 2] return 前 — message.completed."""
    return bool(
        ctrl.on_completed(
            message_id=message_id,
            answer=answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )
    )


@_safe_hook(default_return=False)
def on_tool_updated(
    *,
    ctrl: Any,
    message_id: str,
    tool_name: str,
    status: str,
    detail: str = "",
) -> bool:
    """[注入点 3] progress_callback — tool.updated."""
    ctrl.on_tool_update(
        message_id=message_id,
        tool_name=tool_name,
        status=status,
        detail=detail,
    )
    return True


@_safe_hook(default_return=False, log_level="debug")
def on_answer_delta(*, ctrl: Any, message_id: str, text: str) -> bool:
    """[注入点 4] _stream_delta_cb — answer.delta."""
    ctrl.on_answer(message_id=message_id, text=text)
    return True


@_safe_hook(default_return=False, log_level="debug")
def on_thinking_delta(*, ctrl: Any, message_id: str, text: str) -> bool:
    """[注入点 5] _interim_assistant_cb — thinking.delta."""
    ctrl.on_thinking(message_id=message_id, text=text)
    return True


@_safe_hook(default_return=False, log_level="debug")
def on_reasoning_delta(*, ctrl: Any, message_id: str, text: str) -> bool:
    """[注入点 6] reasoning_callback — native model reasoning delta."""
    ctrl.on_reasoning(message_id=message_id, text=text)
    return True


@_safe_hook(default_return=False, log_level="debug")
def on_background_review_message(
    *,
    ctrl: Any,
    message_id: str,
    text: str,
    sender: Callable[[str], Any],
) -> bool:
    """[注入点 7] background_review_callback — background.review."""
    deferred: bool = ctrl.defer_background_review(message_id=message_id, text=text, sender=sender)
    return deferred


@_safe_hook()
def on_message_aborted(*, ctrl: Any, message_id: str) -> None:
    """[注入点 8] stale return None 前 — message.aborted."""
    ctrl.on_aborted(message_id=message_id)


@_safe_hook()
def on_message_interrupted(
    *,
    ctrl: Any,
    message_id: str,
    new_message_id: str,
    chat_id: str,
    anchor_id: str | None = None,
) -> None:
    """[注入点 9] interrupt 发生 — message.interrupted."""
    ctrl.on_interrupted(
        old_message_id=message_id,
        new_message_id=new_message_id,
        chat_id=chat_id,
        anchor_id=anchor_id,
    )
