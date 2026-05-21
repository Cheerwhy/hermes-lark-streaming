"""线性单卡模式的异步 API 编排 — 创建、刷新、完成."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from .cardkit import (
    _LOADING_ELEMENT_ID,
    _build_reasoning_panel,
    _build_tool_panel,
    _format_elapsed,
    _streaming_element,
    build_im_fallback_card,
    build_linear_complete_card,
    build_streaming_card_v2,
)
from .cardkit_i18n import _T, _i18n
from .cardkit_md import (
    _downgrade_tables,
    optimize_markdown_style,
)
from .controller_mixin import (
    _TERMINAL,
    ABORTED,
    COMPLETED,
    CREATING,
    FAILED,
    IDLE,
    STREAMING,
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
from .linear import LinearState, Segment
from .text import split_reasoning_text

if TYPE_CHECKING:
    from .config import Config
    from .controller import CardSession
    from .feishu import FeishuClient

_logger = logging.getLogger("hermes_lark_streaming")


class LinearControllerMixin:
    """线性模式专用方法 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[..., Coroutine[Any, Any, None]]
    _schedule_card_update: Callable[[CardSession], None]
    _cleanup: Callable[[str], None]
    _flush_deferred_background_reviews: Callable[[CardSession], None]
    _do_complete_inner: Callable[..., Coroutine[Any, Any, bool]]

    def _schedule_linear_flush(self, session: CardSession) -> None:
        if session.state == IDLE or session.state in _TERMINAL:
            return
        if session.guard.should_skip("_schedule_linear_flush"):
            return
        session.flush.schedule_update(lambda: self._do_linear_flush(session))

    def _linear_on_thinking(self, session: CardSession, text: str) -> None:
        linear_state = session.linear_state
        if linear_state is None:
            return
        split = split_reasoning_text(text)
        reasoning = split.get("reasoning_text")
        answer = split.get("answer_text")

        if reasoning and self._cfg.show_reasoning:
            linear_state.on_reasoning_delta(reasoning)
        if answer:
            linear_state.on_answer_delta(answer)
        if not (reasoning and self._cfg.show_reasoning) and not answer:
            return
        self._schedule_linear_flush(session)

    async def _do_create_linear_card(self, session: CardSession) -> None:
        """线性模式：创建只有 loading 的占位卡片."""
        if session.state != IDLE:
            return
        session.state = CREATING
        session.linear = True
        session.linear_state = LinearState()

        try:
            await self._ensure_init()
            assert self._client is not None

            try:
                card = build_streaming_card_v2(
                    show_tool_use=False,
                    show_reasoning=False,
                    show_streaming_element=False,
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
                _logger.info("linear CardKit create failed, falling back to non-linear")
                card = build_im_fallback_card()
                card_msg_id = await self._client.reply_card(
                    session.message_id,
                    card,
                )
                session.card_msg_id = card_msg_id
                session.use_cardkit = False
                session.linear = False
                session.linear_state = None
                session.flush.set_throttle(PATCH_MS)

            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=(
                        lambda: self._schedule_linear_flush(session)
                        if session.linear
                        else self._schedule_card_update(session)
                    ),
                )

            session.flush.set_card_message_ready(True)
            if session.state == CREATING:
                session.state = STREAMING
            if session.linear and session.linear_state and session.linear_state.has_dirty:
                self._schedule_linear_flush(session)
            _logger.info(
                "linear card created: msg=%s linear=%s card_id=%s",
                session.message_id[:12],
                session.linear,
                (session.card_id or "")[:12],
            )
        except Exception:
            _logger.exception("_do_create_linear_card failed")
            session.state = FAILED

    async def _do_linear_flush(self, session: CardSession) -> None:
        """线性模式幂等 flush：batch_update 结构变更 → stream 文本 → batch_update tool 面板."""
        if session.state in _TERMINAL or not session.card_id:
            return
        linear_state = session.linear_state
        if linear_state is None:
            return

        assert self._client is not None
        segments = linear_state.segments
        all_steps = session.tool_use.build_display_steps()

        # ── 步骤 1: batch_update — 创建元素 + reasoning 标题 ──
        step1_actions: list[dict[str, Any]] = []
        new_el_ids: set[str] = set()

        # 1a: add_elements 创建 reasoning + answer 元素（tool 延迟到步骤 3）
        for seg in segments:
            if seg.created or seg.type == "tool":
                continue
            new_el_ids.add(seg.el_id)
            if seg.type == "reasoning":
                panel = _build_reasoning_panel(
                    " ",
                    seg.elapsed_ms,
                    expanded=True,
                    element_id=seg.el_id,
                    text_element_id=seg.text_el_id,
                )
                step1_actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": _LOADING_ELEMENT_ID,
                        "elements": [panel],
                    },
                })
            elif seg.type == "answer":
                el = _streaming_element(element_id=seg.el_id)
                step1_actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": _LOADING_ELEMENT_ID,
                        "elements": [el],
                    },
                })

        # 1b: partial_update_element 更新已创建 reasoning 的标题
        for seg in segments:
            if seg.type != "reasoning" or not seg.created:
                continue
            if seg.elapsed_ms <= 0 or seg.reasoning_finalized:
                continue
            _logger.info(
                "linear reasoning finalize: msg=%s el=%s elapsed=%.0fms seq=%d",
                session.message_id[:12],
                seg.el_id,
                seg.elapsed_ms,
                session.sequence + 1,
            )
            d = _format_elapsed(seg.elapsed_ms)
            en_label = _T["thought_for"][0].format(d)
            zh_label = _T["thought_for"][1].format(d)
            partial = {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"💭 {en_label}",
                        "i18n_content": _i18n(f"💭 {en_label}", f"💭 {zh_label}"),
                        "text_color": "grey",
                        "text_size": "notation",
                    },
                },
            }
            step1_actions.append({
                "action": "partial_update_element",
                "params": {"element_id": seg.el_id, "partial_element": partial},
            })

        if step1_actions:
            session.sequence += 1
            _logger.debug(
                "linear flush step1: msg=%s seq=%d actions=%d",
                session.message_id[:12],
                session.sequence,
                len(step1_actions),
            )
            pre_flush_reasoning_elapsed = {seg.el_id: seg.elapsed_ms for seg in segments if seg.type == "reasoning"}
            try:
                await self._client.cardkit_batch_update(
                    session.card_id,
                    step1_actions,
                    sequence=session.sequence,
                )
                for seg in segments:
                    if seg.el_id in new_el_ids:
                        seg.created = True
                for seg in segments:
                    if seg.type == "reasoning" and pre_flush_reasoning_elapsed.get(seg.el_id, 0) > 0:
                        seg.reasoning_finalized = True
                if new_el_ids:
                    for seg in segments:
                        if seg.el_id in new_el_ids or not seg.created:
                            continue
                        if seg.type in ("reasoning", "answer") and seg.text:
                            seg.dirty = True
            except FeishuAPIError as e:
                _logger.debug("linear batch update step1 failed: %s", e, exc_info=True)
                self._handle_linear_flush_error(session, e)
                return

        # ── 步骤 2: stream_element 刷脏文本 ──
        for seg in segments:
            if not seg.created or not seg.dirty:
                continue
            try:
                if seg.type == "reasoning":
                    content = optimize_markdown_style(seg.text) or " "
                    session.sequence += 1
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.text_el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
                elif seg.type == "answer":
                    content = seg.text
                    if session.image_resolver:
                        content = session.image_resolver.resolve_images(content)
                    content = _downgrade_tables(optimize_markdown_style(content)) or " "
                    session.sequence += 1
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
            except Exception as e:
                _logger.debug("linear stream failed: %s el=%s", e, seg.el_id, exc_info=True)

        # ── 步骤 3: batch_update — 创建 tool 元素 + 更新 tool 面板 ──
        step3_actions: list[dict[str, Any]] = []
        new_tool_ids: set[str] = set()
        step3_dirty_segs: list[Segment] = []

        for seg in segments:
            if seg.type != "tool":
                continue
            if not seg.created:
                new_tool_ids.add(seg.el_id)
                start = seg.tool_offset
                end = seg.tool_end_offset if seg.tool_end_offset else len(all_steps)
                panel = _build_tool_panel(all_steps[start:end], element_id=seg.el_id)
                step3_actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": _LOADING_ELEMENT_ID,
                        "elements": [panel],
                    },
                })
                step3_dirty_segs.append(seg)
            elif seg.dirty:
                if seg.tool_end_offset > 0:
                    start = seg.tool_offset
                    end = seg.tool_end_offset
                else:
                    start = seg.tool_offset
                    end = len(all_steps)
                steps_slice = all_steps[start:end]
                panel = _build_tool_panel(steps_slice)
                step3_actions.append({
                    "action": "partial_update_element",
                    "params": {
                        "element_id": seg.el_id,
                        "partial_element": {
                            "elements": panel["elements"],
                            "header": panel["header"],
                        },
                    },
                })
                step3_dirty_segs.append(seg)

        pre_flush_offsets = {seg.el_id: seg.tool_end_offset for seg in segments if seg.type == "tool"}

        if step3_actions:
            session.sequence += 1
            _logger.info(
                "linear tool update: msg=%s seq=%d actions=%d created=%d updated=%d",
                session.message_id[:12],
                session.sequence,
                len(step3_actions),
                len(new_tool_ids),
                len(step3_dirty_segs) - len(new_tool_ids),
            )
            try:
                await self._client.cardkit_batch_update(
                    session.card_id,
                    step3_actions,
                    sequence=session.sequence,
                )
                for seg in segments:
                    if seg.el_id in new_tool_ids:
                        seg.created = True
                for seg in step3_dirty_segs:
                    if pre_flush_offsets.get(seg.el_id, -1) == seg.tool_end_offset and seg.tool_end_offset > 0:
                        seg.dirty = False
            except FeishuAPIError as e:
                _logger.warning(
                    "linear tool update failed: msg=%s seq=%d code=%s",
                    session.message_id[:12],
                    session.sequence,
                    e.code,
                )

    def _handle_linear_flush_error(self, session: CardSession, e: FeishuAPIError) -> None:
        if e.code == CARDKIT_RATE_LIMITED:
            return
        if e.code == CARDKIT_STREAMING_CLOSED:
            return
        if e.code == CARDKIT_CONTENT_FAILED:
            sub_code = e.extract_sub_code()
            if sub_code == CARDKIT_ELEMENT_LIMIT:
                _logger.warning("linear card element limit exceeded")

    async def _do_linear_complete(self, session: CardSession) -> bool:
        """线性模式完成：close streaming + 全量重建卡片（保持 segments 顺序）."""
        try:
            return await self._do_linear_complete_inner(session)
        finally:
            self._flush_deferred_background_reviews(session)
            self._cleanup(session.message_id)

    async def _do_linear_complete_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_linear_complete"):
            return False

        await session.flush.wait_for_flush()
        session.flush.mark_completed()

        linear_state = session.linear_state
        is_error = session.state == FAILED
        is_aborted = session.state == ABORTED
        all_tool_steps = session.tool_use.build_display_steps()

        if linear_state is not None:
            linear_state.finalize_segments(len(all_tool_steps))

        if session.image_resolver:
            for seg in (linear_state.segments if linear_state is not None else []):
                if seg.type == "answer" and seg.text:
                    try:
                        seg.text = await session.image_resolver.resolve_await(seg.text)
                    except Exception:
                        _logger.debug("linear image resolve failed: el=%s", seg.el_id, exc_info=True)

        card = build_linear_complete_card(
            segments=linear_state.segments if linear_state is not None else [],
            all_tool_steps=all_tool_steps,
            footer_data=session.footer,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
        )

        streaming_closed = False
        for attempt in range(3):
            try:
                assert self._client is not None
                if session.card_id:
                    if not streaming_closed:
                        session.sequence += 1
                        await self._client.cardkit_close_streaming(
                            session.card_id,
                            sequence=session.sequence,
                        )
                        streaming_closed = True
                    session.sequence += 1
                    await self._client.cardkit_update(
                        session.card_id,
                        card,
                        sequence=session.sequence,
                    )
                session.state = COMPLETED
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "linear complete attempt %d failed: code=%s msg=%s card_id=%s seq=%d",
                    attempt,
                    e.code,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if session.guard.terminate("_do_linear_complete", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "linear complete attempt %d failed: %s: %s card_id=%s seq=%d",
                    attempt,
                    type(e).__name__,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue

        _logger.error(
            "linear complete failed after 3 attempts: card_id=%s seq=%d",
            session.card_id,
            session.sequence,
        )
        session.state = FAILED
        return False
