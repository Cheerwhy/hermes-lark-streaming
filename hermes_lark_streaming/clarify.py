"""Feishu clarify-as-card: render the ``clarify`` tool prompt as a native
Feishu interactive card with choice buttons instead of a numbered text list.

This mirrors the adapter's existing ``send_exec_approval`` / ``hermes_action``
card-action pattern, but for clarify.  It builds on Hermes's canonical
clarify primitive in ``tools.clarify_gateway`` (register / wait_for_response
/ resolve_gateway_clarify) — no custom polling or state machine here.

Wiring (installed by ``patcher.FeishuAdapterPatcher`` into
``gateway/platforms/feishu.py``):

  * ``send_clarify`` override on the Feishu adapter → :func:`send_clarify_card`
    builds the card (markdown question + ``tag:action`` choice buttons + an
    "Other" free-text button) and sends it as a plain ``interactive`` message.
  * clarify branch in ``_on_card_action_trigger`` → :func:`handle_clarify_card_action`
    resolves the pending clarify (``resolve_gateway_clarify`` for a choice,
    ``mark_awaiting_text`` for "Other") and returns an inline updated card.

The blocking/resolution is handled entirely by ``_clarify_callback_sync`` in
``gateway/run.py`` + ``clarify_gateway`` — this module only customises the
*rendering* (card vs text) and the *callback dispatch*.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("hermes_lark_streaming")

# Match tools.clarify_tool.MAX_CHOICES — the agent offers at most 4 predefined
# choices; a 5th "Other" (free-text) button is always appended by this module.
MAX_CHOICE_BUTTONS = 4

# Action value keys (mirrors the ``hermes_action`` / ``hermes_update_prompt_action``
# convention used by approval / update-prompt cards).
_CLARIFY_KEY = "hermes_clarify"
_ACTION_CHOOSE = "choose"
_ACTION_OTHER_SUBMIT = "other_submit"

# Store original questions so resolved cards can display context.
# Keyed by clarify_id, cleaned up on resolve.
_clarify_questions: dict[str, str] = {}


# ---------------------------------------------------------------------------
# lark-oapi response classes (same guarded import as the adapter)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised in the adapter process, not in unit tests
    from lark_oapi.event.callback.model.p2_card_action_trigger import (  # type: ignore
        CallBackCard,
        P2CardActionTriggerResponse,
    )
except Exception:  # pragma: no cover
    CallBackCard = None  # type: ignore[assignment]
    P2CardActionTriggerResponse = None  # type: ignore[assignment]


def _empty_response() -> Any:
    """Return the SDK's empty card-action response (or None if unavailable)."""
    return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None


def _card_action_response(card_data: dict[str, Any]) -> Any:
    """Wrap a card dict in P2CardActionTriggerResponse for inline update."""
    if P2CardActionTriggerResponse is None:
        return None
    response = P2CardActionTriggerResponse()
    if CallBackCard is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = card_data
        response.card = card
    return response


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------
def build_clarify_card(
    *,
    question: str,
    choices: list[str] | None,
    clarify_id: str,
) -> dict[str, Any]:
    """Build the Feishu interactive card for a clarify prompt.

    * ``choices`` non-empty → one button per choice (first is ``primary``).
      Each button ``value`` carries the ``clarify_id`` so the card-action
      callback can resolve it.  An input element with callback behavior is
      also included for free-text entry.
    * ``choices`` empty/None → open-ended prompt, no buttons (the gateway's
      text-intercept captures the next message — ``awaiting_text`` is already
      True from ``clarify_gateway.register``).
    """
    prompt = (question or "").strip() or "请选择"
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": prompt}]

    if choices:
        buttons: list[dict[str, Any]] = []
        for idx, choice in enumerate(choices[:MAX_CHOICE_BUTTONS]):
            label = str(choice).strip()
            if not label:
                continue
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if idx == 0 else "default",
                    "value": {
                        _CLARIFY_KEY: _ACTION_CHOOSE,
                        "clarify_id": clarify_id,
                        "choice": label,
                    },
                }
            )
        if buttons:
            elements.extend(buttons)

    # Divider between buttons and input
    elements.append({"tag": "hr"})

    # Input with behaviors — input's built-in submit icon triggers callback
    # behaviors.value carries hermes_clarify so the adapter's clarify check matches
    # action.input_value carries the user's typed text
    elements.append(
        {
            "tag": "input",
            "name": "clarify_text",
            "placeholder": {"tag": "plain_text", "content": "输入自定义回答后点提交图标..."},
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        _CLARIFY_KEY: _ACTION_OTHER_SUBMIT,
                        "clarify_id": clarify_id,
                    },
                }
            ],
        }
    )

    return {
        "schema": "2.0",
        "header": {
            "title": {"content": "❓ 需要澄清", "tag": "plain_text"},
            "template": "orange",
        },
        "body": {"elements": elements},
    }


def build_clarify_resolved_card(
    *,
    question: str,
    chosen: str | None = None,
    awaiting_text: bool = False,
) -> dict[str, Any]:
    """Build the inline replacement card shown after a button click.

    ``chosen`` set → green "已选择" card showing the chosen answer.
    ``awaiting_text`` → blue card prompting the user to type a free-form answer.
    """
    prompt = (question or "").strip()
    if chosen is not None:
        body = f"{prompt}\n\n**已选择：** {chosen}" if prompt else f"**已选择：** {chosen}"
        return {
            "schema": "2.0",
            "header": {
                "title": {"content": "✅ 已选择", "tag": "plain_text"},
                "template": "green",
            },
            "body": {"elements": [{"tag": "markdown", "content": body}]},
        }
    if awaiting_text:
        body = f"{prompt}\n\n请在对话中输入你的回答" if prompt else "请在对话中输入你的回答"
        return {
            "schema": "2.0",
            "header": {
                "title": {"content": "✍️ 请输入回答", "tag": "plain_text"},
                "template": "blue",
            },
            "body": {"elements": [{"tag": "markdown", "content": body}]},
        }
    # Fallback (shouldn't happen) — neutral confirmation.
    return {
        "schema": "2.0",
        "header": {"title": {"content": "✓", "tag": "plain_text"}, "template": "grey"},
        "body": {"elements": [{"tag": "markdown", "content": prompt or "已处理"}]},
    }


# ---------------------------------------------------------------------------
# send_clarify override (called from the injected adapter method)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Session rotation: complete old streaming card, create new session
# ---------------------------------------------------------------------------
async def _rotate_streaming_session(chat_id: str) -> None:
    """Complete the current streaming card and create a new session.

    Called from ``_rotate_and_resolve`` before resolving the clarify. This
    ensures that post-clarify agent output goes to a new streaming card
    instead of appending to the old one.

    Must be async because ``_do_complete_card`` runs on the event loop and
    its ``finally`` clause calls ``_cleanup(message_id)``.  We must await
    completion (including cleanup) BEFORE creating the new session — otherwise
    the fire-and-forget cleanup would delete the new session (same message_id).
    """
    from .controller import get_controller
    from .streaming.session import CardSession

    ctrl = get_controller()

    # Find active session for this chat_id
    active_session = None
    for session in ctrl._sessions.values():
        if getattr(session, "chat_id", "") == chat_id and not session.state.is_terminal:
            active_session = session
            break

    if active_session is None:
        return

    message_id = active_session.message_id
    anchor_id = active_session.anchor_id
    loop = active_session._loop

    active_session.flush.mark_completed()
    try:
        await ctrl._do_complete_card(active_session)
    except Exception:
        # Ensure old session is cleaned up even if _do_complete_card failed
        if ctrl._sessions.get(message_id) is active_session:
            ctrl._cleanup(message_id)

    # Now safe to create new session — _do_complete_card's _cleanup already ran
    new_session = CardSession(message_id, chat_id, loop)
    if anchor_id:
        new_session.anchor_id = anchor_id
        ctrl._sessions[anchor_id] = new_session
    ctrl._sessions[message_id] = new_session

    # Create the new streaming card and AWAIT it.
    try:
        await ctrl._do_create_card(new_session)
    except Exception:
        logger.warning("Failed to create new card during rotation", exc_info=True)

    logger.info(
        "Rotated streaming card for clarify: msg=%s chat=%s",
        message_id[:12],
        chat_id[:12],
    )


async def send_clarify_card(
    adapter: Any,
    *,
    chat_id: str,
    question: str,
    choices: list[str] | None,
    clarify_id: str,
    session_key: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Send the clarify interactive card.

    Before sending, finalizes the current streaming card session and creates
    a new one, so post-clarify agent output goes to a fresh card instead of
    appending to the old one.
    """
    # No rotation here — the old streaming card stays alive while the
    # clarify card is displayed.  Rotation happens AFTER the user clicks
    # a button (in handle_clarify_card_action), so the user sees the
    # clarify card first, and only then does the old card close and a
    # new one open for post-clarify output.
    card = build_clarify_card(
        question=question, choices=choices, clarify_id=clarify_id
    )
    _clarify_questions[clarify_id] = question or ""
    # 用 CardKit API 发送（input 元素只在 CardKit 卡片里渲染）
    try:
        from .controller import get_controller
        ctrl = get_controller()
        if ctrl._client is not None:
            card_id = await ctrl._client.cardkit_create(card)
            result_msg_id = await ctrl._client.send_card_to_chat(
                chat_id=chat_id,
                card={"type": "card", "data": {"card_id": card_id}},
            )
            try:
                from gateway.platforms.base import SendResult
                return SendResult(success=True, message_id=result_msg_id, error=None)
            except ImportError:
                import types
                return types.SimpleNamespace(success=True, message_id=result_msg_id, error=None)
    except Exception as e:
        logger.warning("CardKit send failed for clarify, falling back to im API: %s", e)
    # Fallback: im API（input 不渲染，但卡片能发）
    payload = json.dumps(card, ensure_ascii=False)
    response = await adapter._feishu_send_with_retry(
        chat_id=chat_id,
        msg_type="interactive",
        payload=payload,
        reply_to=None,
        metadata=metadata,
    )
    return adapter._finalize_send_result(response, "send_clarify_card failed")


# ---------------------------------------------------------------------------
# Card-action callback handler (called from the injected _on_card_action_trigger branch)
# ---------------------------------------------------------------------------
def _action_value(action_value: Any) -> dict[str, Any]:
    if isinstance(action_value, dict):
        return action_value
    return {}


def handle_clarify_card_action(
    adapter: Any,
    *,
    event: Any,
    action_value: Any,
    loop: Any,
) -> Any:
    """Synchronous card-action handler for clarify button clicks.

    Returns the inline replacement card immediately (so all Feishu clients
    update instantly), then schedules an async task that:
      1. Rotates the streaming card session (complete old → create new)
      2. Resolves the pending clarify (unblocks the agent)

    The rotation MUST complete before resolve, otherwise post-clarify output
    has no session to stream to.  Scheduling them as one coroutine guarantees
    ordering.

    Stale clicks (already-resolved / timed-out / unknown clarify_id) are
    dropped silently.
    """
    value = _action_value(action_value)
    if not value.get(_CLARIFY_KEY):
        return _empty_response()

    clarify_id = str(value.get("clarify_id") or "")
    action = str(value.get(_CLARIFY_KEY) or "")
    if not clarify_id:
        logger.debug("[Feishu] clarify card action missing clarify_id, ignoring")
        return _empty_response()

    # Operator authorisation — same gate as approval/update-prompt.
    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    if (
        hasattr(adapter, "_is_interactive_operator_authorized")
        and not adapter._is_interactive_operator_authorized(open_id)
    ):
        logger.warning(
            "[Feishu] Unauthorized clarify click by %s", open_id or "<unknown>"
        )
        return _empty_response()

    # chat_id is in event.context.open_chat_id (same as approval handler)
    chat_id = str(
        getattr(getattr(event, "context", None), "open_chat_id", "") or ""
    )

    chosen: str | None = None
    awaiting_text = False
    if action == _ACTION_CHOOSE:
        chosen = str(value.get("choice") or "")
        if not chosen:
            return _empty_response()
        # Schedule: rotate session → resolve clarify (order matters!)
        adapter._submit_on_loop(
            loop,
            _rotate_and_resolve(chat_id, clarify_id, chosen),
        )
    elif action == _ACTION_OTHER_SUBMIT:
        # Extract text from input_value (v2 card) or form_value (v1 fallback)
        action_obj = getattr(event, "action", None)
        chosen = str(getattr(action_obj, "input_value", None) or "").strip()
        if not chosen:
            form_value = getattr(action_obj, "form_value", None) or {}
            chosen = str(form_value.get("clarify_text") or "").strip()
        if not chosen:
            return _empty_response()
        adapter._submit_on_loop(
            loop,
            _rotate_and_resolve(chat_id, clarify_id, chosen),
        )
    else:
        return _empty_response()

    if P2CardActionTriggerResponse is None:
        return _empty_response()
    question = _clarify_questions.pop(clarify_id, "")
    response = P2CardActionTriggerResponse()
    if CallBackCard is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = build_clarify_resolved_card(
            question=question, chosen=chosen, awaiting_text=awaiting_text
        )
        response.card = card
    return response


async def _rotate_and_resolve(
    chat_id: str, clarify_id: str, choice: str
) -> None:
    """Rotate streaming session, then resolve the clarify.

    Rotation must finish before resolve so the new session is ready
    when the agent unblocks.
    """
    from tools.clarify_gateway import resolve_gateway_clarify
    if chat_id:
        await _rotate_streaming_session(chat_id)
    resolve_gateway_clarify(clarify_id, choice)



