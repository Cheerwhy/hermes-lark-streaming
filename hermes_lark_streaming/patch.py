"""AST Patch 注入的 Hook 函数.

这些函数从 gateway/run.py 的 7 个注入点被调用.
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


@_safe_hook()
def on_message_started(*, ctrl: Any, message_id: str, chat_id: str) -> None:
    """[注入点 1] 函数开头 — message.started."""
    ctrl.on_message_started(message_id=message_id, chat_id=chat_id)


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


@_safe_hook()
def on_message_aborted(*, ctrl: Any, message_id: str) -> None:
    """[注入点 6] stale return None 前 — message.aborted."""
    ctrl.on_aborted(message_id=message_id)


@_safe_hook()
def on_message_interrupted(
    *,
    ctrl: Any,
    message_id: str,
    new_message_id: str,
    chat_id: str,
) -> None:
    """[注入点 7] interrupt 发生 — message.interrupted."""
    ctrl.on_interrupted(
        old_message_id=message_id,
        new_message_id=new_message_id,
        chat_id=chat_id,
    )


@_safe_hook(default_return=False)
def on_cron_deliver(
    *,
    ctrl: Any,
    message_id: str = "",
    platform_name: str,
    chat_id: str,
    content: str,
) -> bool:
    """[Hook 8] cron delivery — wrap as card for Feishu."""
    return ctrl.on_cron_deliver(
        platform_name=platform_name,
        chat_id=chat_id,
        content=content,
    )
