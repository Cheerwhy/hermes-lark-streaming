"""Feishu model picker: render the ``/model`` command as a native Feishu
interactive card with a dropdown selector instead of a text list.

Wiring (installed by ``patcher.FeishuAdapterPatcher`` into
``gateway/platforms/feishu.py``):

  * ``send_model_picker`` override on the Feishu adapter → sends a blue-header
    card with a ``select_static`` dropdown listing all available models.
  * model_picker branch in ``_on_card_action_trigger`` → calls the
    ``on_model_selected`` callback, returns an inline updated green card.

The model switch logic is handled entirely by ``slash_commands.py``'s
``_on_model_selected`` closure — this module only customises the rendering.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger("hermes_lark_streaming")

# lark-oapi response classes (same guarded import as clarify.py)
try:  # pragma: no cover - exercised in the adapter process, not in unit tests
    from lark_oapi.event.callback.model.p2_card_action_trigger import (  # type: ignore
        CallBackCard,
        P2CardActionTriggerResponse,
    )
except Exception:  # pragma: no cover
    CallBackCard = None  # type: ignore[assignment]
    P2CardActionTriggerResponse = None  # type: ignore[assignment]


def _empty_response() -> Any:
    return P2CardActionTriggerResponse() if P2CardActionTriggerResponse else None


# ---------------------------------------------------------------------------
# Option builders
# ---------------------------------------------------------------------------
def _model_picker_options(
    providers: Any,
    *,
    current_model: str = "",
    max_options: int = 24,
) -> list[dict[str, Any]]:
    """Build select_static options from providers list."""
    options: list[dict[str, Any]] = []
    if not isinstance(providers, list):
        return options
    current = str(current_model or "").strip()
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_slug = str(
            provider.get("slug") or provider.get("provider") or ""
        ).strip()
        provider_name = str(
            provider.get("name") or provider_slug or "provider"
        ).strip()
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            model_id = str(model or "").strip()
            if not model_id:
                continue
            label = f"{provider_name} · {model_id}"
            if model_id == current:
                label = f"当前 · {label}"
            options.append(
                {
                    "text": {
                        "tag": "plain_text",
                        "content": label[:80],
                    },
                    "value": json.dumps(
                        {"provider": provider_slug, "model": model_id},
                        ensure_ascii=False,
                    ),
                }
            )
            if len(options) >= max_options:
                return options
    return options


def _parse_model_picker_choice(choice: str) -> tuple[str, str] | None:
    """Parse a select_static choice into (provider_slug, model_id)."""
    try:
        data = json.loads(choice)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    if not provider or not model:
        return None
    return provider, model


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------
def build_model_picker_card(
    *,
    providers: Any,
    current_model: str,
    current_provider: str,
    picker_id: str,
) -> dict[str, Any] | None:
    """Build the model picker card with a select_static dropdown."""
    all_options = _model_picker_options(providers, current_model=current_model, max_options=1000)
    all_count = len(all_options)
    options = all_options[:24]
    if not options:
        return None

    description_parts: list[str] = []
    if current_model:
        description_parts.append(f"当前模型：`{current_model}`")
    if current_provider:
        description_parts.append(f"当前 provider：`{current_provider}`")
    if all_count > len(options):
        description_parts.append(
            f"展示前 {len(options)} 个可选模型，"
            f"可继续用 `/model <模型名>` 精确切换。"
        )

    initial_option = ""
    for opt in options:
        opt_value = str(opt.get("value") or "")
        if current_model and opt_value and current_model in opt_value:
            initial_option = opt_value
            break

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": "选择模型", "tag": "plain_text"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "\n".join(description_parts) or "请选择模型。",
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "select_static",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "选择模型",
                        },
                        "value": {
                            "hfc_action": "model_picker",
                            "hfc_model_picker_id": picker_id,
                        },
                        "options": options,
                        "initial_option": initial_option,
                    }
                ],
            },
        ],
    }


def build_model_picker_resolved_card(
    *,
    result_text: str,
    success: bool = True,
) -> dict[str, Any]:
    """Build the inline replacement card after model selection."""
    template = "green" if success else "red"
    title = "模型已更新" if success else "模型切换失败"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"content": title, "tag": "plain_text"},
            "template": template,
        },
        "elements": [{"tag": "markdown", "content": result_text}],
    }


# ---------------------------------------------------------------------------
# send_model_picker (called from injected adapter method)
# ---------------------------------------------------------------------------
async def send_model_picker(
    adapter: Any,
    *,
    chat_id: str,
    providers: Any,
    current_model: str,
    current_provider: str,
    session_key: str,
    on_model_selected: Any,
    metadata: dict[str, Any] | None = None,
) -> Any:
    """Send the model picker card with a dropdown selector."""
    picker_id = "model_" + hashlib.sha256(
        f"{chat_id}:{session_key}:{time.time()}".encode()
    ).hexdigest()[:16]

    card = build_model_picker_card(
        providers=providers,
        current_model=current_model,
        current_provider=current_provider,
        picker_id=picker_id,
    )
    if card is None:
        return adapter._finalize_send_result(
            None, "send_model_picker: no model options"
        )

    payload = json.dumps(card, ensure_ascii=False)
    response = await adapter._feishu_send_with_retry(
        chat_id=chat_id,
        msg_type="interactive",
        payload=payload,
        reply_to=None,
        metadata=metadata,
    )
    result = adapter._finalize_send_result(
        response, "send_model_picker failed"
    )

    # Store picker state on adapter instance
    if not hasattr(adapter, "_hfc_model_picker_state"):
        adapter._hfc_model_picker_state = {}
    message_id = getattr(result, "message_id", "") or ""
    adapter._hfc_model_picker_state[picker_id] = {
        "chat_id": str(chat_id or ""),
        "session_key": str(session_key or ""),
        "message_id": message_id,
        "on_model_selected": on_model_selected,
    }

    logger.info(
        "Model picker card sent: picker_id=%s message_id=%s",
        picker_id,
        message_id,
    )
    return result


# ---------------------------------------------------------------------------
# Card-action callback handler
# ---------------------------------------------------------------------------
def handle_model_picker_action(
    adapter: Any,
    *,
    event: Any,
    action_value: Any,
    loop: Any,
) -> Any:
    """Handle model picker select_static callback.

    Called from the injected ``_on_card_action_trigger`` branch when
    ``action_value`` contains ``hfc_action == "model_picker"``.
    """
    value = action_value if isinstance(action_value, dict) else {}

    picker_id = str(value.get("hfc_model_picker_id") or "")
    if not picker_id:
        return _empty_response()

    # Get picker state
    state = getattr(adapter, "_hfc_model_picker_state", {})
    if not isinstance(state, dict):
        return _empty_response()
    item = state.get(picker_id)
    if not isinstance(item, dict):
        return _empty_response()

    # Operator authorization
    operator = getattr(event, "operator", None)
    open_id = str(getattr(operator, "open_id", "") or "")
    if (
        hasattr(adapter, "_is_interactive_operator_authorized")
        and not adapter._is_interactive_operator_authorized(open_id)
    ):
        logger.warning(
            "[Feishu] Unauthorized model picker click by %s",
            open_id or "<unknown>",
        )
        return _empty_response()

    # For select_static, the selected option's value is in event.action.option
    # (NOT in action_value — action_value only has the select_static's own
    # value dict with hfc_action / hfc_model_picker_id).
    action = getattr(event, "action", None)
    choice = str(getattr(action, "option", "") or "")
    selected = _parse_model_picker_choice(choice)
    if selected is None:
        # Invalid choice — return error card
        if P2CardActionTriggerResponse is None:
            return None
        response = P2CardActionTriggerResponse()
        if CallBackCard is not None:
            card = CallBackCard()
            card.type = "raw"
            card.data = build_model_picker_resolved_card(
                result_text="模型选择无效，请重新发送 `/model`。",
                success=False,
            )
            response.card = card
        return response

    provider_slug, model_id = selected
    callback = item.get("on_model_selected")

    try:
        if callback is None:
            result_text = f"已选择 {provider_slug}/{model_id}"
        else:
            # callback is async — schedule on loop and wait
            future = asyncio.run_coroutine_threadsafe(
                callback(
                    str(item.get("chat_id") or ""),
                    model_id,
                    provider_slug,
                ),
                loop,
            )
            result_text = future.result(timeout=30)
    except Exception as exc:
        result_text = f"模型切换失败：{exc}"

    # Clean up state
    state.pop(picker_id, None)

    # Return updated card
    if P2CardActionTriggerResponse is None:
        return None
    response = P2CardActionTriggerResponse()
    if CallBackCard is not None:
        card = CallBackCard()
        card.type = "raw"
        card.data = build_model_picker_resolved_card(
            result_text=str(
                result_text or f"已选择 {provider_slug}/{model_id}"
            ),
            success=True,
        )
        response.card = card
    return response
