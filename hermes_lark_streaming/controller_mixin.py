"""异步卡片 API 编排 — 创建、更新、完成卡片的重试/降级逻辑."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from .cardkit import (
    REASONING_TEXT_ELEMENT_ID,
    STREAMING_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_tool_panel,
    build_complete_card,
    build_im_fallback_card,
    build_streaming_card,
    build_streaming_card_v2,
)
from .cardkit_md import (
    _downgrade_tables,
    optimize_markdown_style,
)
from .feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_RATE_LIMITED,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
)
from .flush import CARDKIT_MS, PATCH_MS
from .image import ImageResolver

if TYPE_CHECKING:
    from .config import Config
    from .controller import CardSession
    from .feishu import FeishuClient

_logger = logging.getLogger("hermes_lark_streaming")

# ── Session state constants ──────────────────────────────────────────────────

IDLE = "idle"
CREATING = "creating"
STREAMING = "streaming"
COMPLETED = "completed"
FAILED = "failed"
ABORTED = "aborted"

_TERMINAL = {COMPLETED, FAILED, ABORTED}


class ControllerMixin:
    """异步卡片 API 操作 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[[], Coroutine[Any, Any, None]]
    _schedule_card_update: Callable[[CardSession], None]
    _cleanup: Callable[[str], None]

    async def _do_create_card(self, session: CardSession) -> None:
        if session.state != IDLE:
            return
        session.state = CREATING

        try:
            await self._ensure_init()
            assert self._client is not None
            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_card_update(session),
                )

            try:
                card = build_streaming_card_v2(
                    show_tool_use=False, show_reasoning=self._cfg.show_reasoning
                )
                card_id = await self._client.cardkit_create(card)
                card_msg_id = await self._client.reply_card_by_id(
                    session.message_id,
                    card_id,
                )
                session.card_id = card_id
                session.card_msg_id = card_msg_id
                session.use_cardkit = True
                session.flush.set_throttle(CARDKIT_MS)
            except FeishuAPIError:
                card = build_im_fallback_card()
                card_msg_id = await self._client.reply_card(
                    session.message_id,
                    card,
                )
                session.card_msg_id = card_msg_id
                session.use_cardkit = False
                session.flush.set_throttle(PATCH_MS)

            session.flush.set_card_message_ready(True)
            if session.state == CREATING:
                session.state = STREAMING
            _logger.info(
                "card created: msg=%s cardkit=%s card_id=%s",
                session.message_id[:12],
                session.use_cardkit,
                (session.card_id or "")[:12],
            )
        except Exception:
            _logger.exception("_do_create_card failed")
            session.state = FAILED

    async def _do_update_card(self, session: CardSession) -> None:
        if session.state not in (CREATING, STREAMING):
            return
        if not session.card_msg_id:
            return
        if session.guard.should_skip("_do_update_card"):
            return

        display = session.text.display_text
        if not session.text.is_dirty(display) and not session.reasoning_dirty:
            _logger.info(
                "update_card skipped (not dirty): msg=%s len=%d",
                session.message_id[:12],
                len(display),
            )
            return

        if session.image_resolver:
            display = session.image_resolver.resolve_images(display)

        _logger.info(
            "update_card: msg=%s seq=%d len=%d cardkit=%s",
            session.message_id[:12],
            session.sequence + 1,
            len(display),
            session.use_cardkit,
        )

        try:
            assert self._client is not None
            if session.use_cardkit and session.card_id:
                if session.reasoning_dirty and session.reasoning_panel_added:
                    reasoning_content = optimize_markdown_style(session.reasoning_text) or " "
                    session.sequence += 1
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        REASONING_TEXT_ELEMENT_ID,
                        reasoning_content,
                        sequence=session.sequence,
                    )
                    session.reasoning_dirty = False

                optimized = _downgrade_tables(optimize_markdown_style(display))
                session.sequence += 1
                await self._client.cardkit_stream_element(
                    session.card_id,
                    STREAMING_ELEMENT_ID,
                    optimized or " ",
                    sequence=session.sequence,
                )
            else:
                tool_steps = session.tool_use.build_display_steps()
                card = build_streaming_card(
                    tool_steps=tool_steps,
                    reasoning_text=session.reasoning_text if self._cfg.show_reasoning else "",
                    text=display,
                )
                await self._client.update_card(session.card_msg_id, card)

            session.text.mark_flushed(display)
            session.reasoning_dirty = False
        except FeishuAPIError as e:
            if session.guard.terminate("_do_update_card", e):
                return

            if e.code == CARDKIT_RATE_LIMITED:
                _logger.info("rate limited, skipping frame")
                return

            if e.code == CARDKIT_STREAMING_CLOSED:
                _logger.info("streaming mode closed, skipping update: msg=%s", session.message_id[:12])
                return

            if e.code == CARDKIT_CONTENT_FAILED:
                sub_code = e.extract_sub_code()
                if sub_code == CARDKIT_ELEMENT_LIMIT:
                    _logger.warning("card element limit exceeded, disabling CardKit streaming")
                    session.use_cardkit = False
                    session.flush.set_throttle(PATCH_MS)
                    return

            _logger.warning("card update failed: %s", e, exc_info=True)

    async def _do_tool_use_status_update(self, session: CardSession) -> None:
        if not session.card_id or session.state in _TERMINAL:
            return
        try:
            assert self._client is not None
            tool_steps = session.tool_use.build_display_steps()
            panel = _build_tool_panel(
                tool_steps,
                session.tool_use.elapsed_ms,
            )
            if not session.tool_panel_added:
                actions = [
                    {
                        "action": "add_elements",
                        "params": {
                            "type": "insert_before",
                            "target_element_id": STREAMING_ELEMENT_ID,
                            "elements": [panel],
                        },
                    }
                ]
            else:
                actions = [
                    {
                        "action": "update_element",
                        "params": {
                            "element_id": TOOL_PANEL_ELEMENT_ID,
                            "element": panel,
                        },
                    }
                ]
            session.sequence += 1
            _logger.info(
                "tool_update: msg=%s seq=%d action=%s steps=%d",
                session.message_id[:12],
                session.sequence,
                "add" if not session.tool_panel_added else "update",
                len(tool_steps),
            )
            await self._client.cardkit_batch_update(
                session.card_id,
                actions,
                sequence=session.sequence,
            )
            session.tool_panel_added = True
        except Exception as e:
            _logger.debug("tool use status update failed: %s", e, exc_info=True)

    async def _do_reasoning_update(self, session: CardSession) -> None:
        if not session.card_id or session.state in _TERMINAL:
            return
        if not session.reasoning_dirty:
            return
        try:
            assert self._client is not None
            content = optimize_markdown_style(session.reasoning_text) or " "

            session.sequence += 1
            _logger.info(
                "reasoning_update: msg=%s seq=%d len=%d",
                session.message_id[:12],
                session.sequence,
                len(session.reasoning_text),
            )
            await self._client.cardkit_stream_element(
                session.card_id,
                REASONING_TEXT_ELEMENT_ID,
                content,
                sequence=session.sequence,
            )
            session.reasoning_panel_added = True
            session.reasoning_dirty = False
        except Exception as e:
            _logger.debug("reasoning update failed: %s", e, exc_info=True)

    async def _do_complete(self, session: CardSession) -> bool:
        try:
            return await self._do_complete_inner(session)
        finally:
            self._cleanup(session.message_id)

    async def _do_complete_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete"):
            return False

        await session.flush.wait_for_flush()
        session.flush.mark_completed()

        display = session.text.display_text
        _logger.info(
            "do_complete: msg=%s state=%s display_len=%d cardkit=%s seq=%d",
            session.message_id[:12],
            session.state,
            len(display),
            session.use_cardkit,
            session.sequence,
        )
        if session.image_resolver:
            try:
                display = await session.image_resolver.resolve_await(display)
            except Exception:
                _logger.debug("image resolve failed", exc_info=True)

        reasoning_elapsed_ms = 0.0
        if session.reasoning_start:
            reasoning_elapsed_ms = (time.time() - session.reasoning_start) * 1000

        is_error = session.state == FAILED
        is_aborted = session.state == ABORTED
        card = build_complete_card(
            text=display,
            reasoning_text=session.reasoning_text if self._cfg.show_reasoning else "",
            reasoning_elapsed_ms=reasoning_elapsed_ms,
            tool_steps=session.tool_use.build_display_steps(),
            tool_elapsed_ms=session.tool_use.elapsed_ms,
            footer_data=session.footer,
            has_cardkit=session.use_cardkit,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
        )

        for attempt in range(3):
            try:
                assert self._client is not None
                if session.use_cardkit and session.card_id:
                    await self._client.cardkit_close_streaming(
                        session.card_id,
                        sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                    await self._client.cardkit_update(
                        session.card_id,
                        card,
                        sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                elif session.card_msg_id:
                    await self._client.update_card(session.card_msg_id, card)
                session.state = COMPLETED
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "cardkit complete attempt %d failed (FeishuAPIError): code=%s msg=%s card_id=%s seq=%d",
                    attempt,
                    e.code,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if session.guard.terminate("_do_complete", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "cardkit complete attempt %d failed: %s: %s card_id=%s card_msg_id=%s seq=%d",
                    attempt,
                    type(e).__name__,
                    e,
                    session.card_id,
                    session.card_msg_id,
                    session.sequence,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue

        _logger.error(
            "cardkit complete failed after 3 attempts: card_id=%s card_msg_id=%s seq=%d",
            session.card_id,
            session.card_msg_id,
            session.sequence,
        )
        session.state = FAILED
        return False
