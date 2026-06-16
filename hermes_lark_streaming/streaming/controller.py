"""流式卡片的异步 API 编排 — 创建、刷新、拆卡、完成."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from ..cardkit.builder import build_background_card, build_complete_card, build_cron_card, build_streaming_card_v2
from ..cardkit.markdown import (
    _downgrade_tables,
    optimize_markdown_style,
)
from ..feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_RATE_LIMITED,
    CARDKIT_STREAMING_CLOSED,
    FeishuAPIError,
)
from .diagnostics import compact_ids, extract_missing_element_id, segment_state_for_log, summarize_actions
from .flush import CARDKIT_MS
from .image import ImageResolver
from .segment_helper import (
    ELEMENT_THRESHOLD,
    FOOTER_RESERVE,
    build_add_segment_action,
    build_reasoning_finalized_action,
    build_tool_update_action,
    estimate_segment_elements,
    estimate_tool_elements,
    find_tool_split_offset,
    tool_segment_end,
)
from .segments import Segment, SegmentState, SegmentType
from .session import SessionState
from .text import split_reasoning_text

if TYPE_CHECKING:
    from ..config import Config
    from ..feishu import FeishuClient
    from .session import CardSession
    from .tooluse import ToolDisplayStep

_logger = logging.getLogger("hermes_lark_streaming")


async def _resolve_answer_images(
    segments: list[Segment],
    resolver: ImageResolver,
    *,
    log_prefix: str,
) -> None:
    """解析 answer segment 中的 markdown 图片，并原地更新文本."""
    for seg in segments:
        if seg.type != SegmentType.ANSWER or not seg.text:
            continue
        try:
            seg.text = await resolver.resolve_await(seg.text)
        except Exception:
            _logger.debug("%s image resolve failed: el=%s", log_prefix, seg.el_id, exc_info=True)


class StreamingController:
    """流式卡片专用方法 — 由 StreamCardController 继承."""

    _client: FeishuClient | None
    _cfg: Config
    _ensure_init: Callable[..., Coroutine[Any, Any, None]]
    _cleanup: Callable[[str], None]
    _flush_deferred_background_reviews: Callable[[CardSession], None]

    def _schedule_flush(self, session: CardSession) -> None:
        if session.state == SessionState.IDLE or session.state.is_terminal:
            return
        if session.guard.should_skip("_schedule_flush"):
            return
        session.flush.schedule_update(lambda: self._do_flush(session))

    def _on_thinking_segment(self, session: CardSession, text: str) -> bool:
        segment_state = session.segment_state
        if segment_state is None:
            return False
        split = split_reasoning_text(text)
        reasoning = split.get("reasoning_text")
        answer = split.get("answer_text")

        if reasoning and self._cfg.show_reasoning:
            segment_state.on_reasoning_delta(reasoning)
        if answer:
            segment_state.on_answer_delta(answer)
        if not (reasoning and self._cfg.show_reasoning) and not answer:
            return False
        self._schedule_flush(session)
        return True

    async def _do_create_card(self, session: CardSession) -> None:
        """创建只有 loading 的流式占位卡片."""
        if session.state != SessionState.IDLE:
            return
        session.state = SessionState.CREATING
        if session.segment_state is None:
            session.segment_state = SegmentState()

        try:
            await self._ensure_init()
            assert self._client is not None

            reply_to_message_id = session.anchor_id or session.message_id
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
                header_enabled=self._cfg.header_enabled,
                text_size=self._cfg.body_text_size,
            )
            card_id = await self._client.cardkit_create(card)
            card_msg_id = await self._client.reply_card_by_id(
                reply_to_message_id,
                card_id,
            )
            session.set_card(card_id=card_id, card_msg_id=card_msg_id)
            session.element_count = 1  # loading element
            session.flush.set_throttle(CARDKIT_MS)

            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_flush(session),
                )

            session.flush.set_card_message_ready(True)
            if session.state == SessionState.CREATING:
                session.state = SessionState.STREAMING
            if session.segment_state and session.segment_state.has_dirty:
                self._schedule_flush(session)
            _logger.info(
                "CardKit card created: msg=%s card_id=%s",
                session.message_id[:12],
                (session.card_id or "")[:12],
            )
        except FeishuAPIError:
            _logger.info("CardKit create failed, yielding to gateway", exc_info=True)
            if hasattr(self, "_mark_text_fallback_needed"):
                self._mark_text_fallback_needed(session)
            session.mark_failed()
        except Exception:
            _logger.exception("_do_create_card failed")
            session.mark_failed()

    async def _do_flush(self, session: CardSession) -> None:
        """幂等 flush：按 segment 顺序处理结构性变更，超阈值时拆卡."""
        if session.state.is_terminal or not session.card_id:
            return
        segment_state = session.segment_state
        if segment_state is None:
            return

        assert self._client is not None
        segments = segment_state.segments
        all_steps = session.tool_use.build_display_steps()

        # ── 步骤 1: batch_update — 按 segment 顺序处理结构性变更 ──
        actions: list[dict[str, Any]] = []
        new_el_ids: set[str] = set()
        new_el_estimates: dict[str, int] = {}
        updated_tool_segs: list[Segment] = []
        new_el_total = 0  # 同一 flush 内新 segment 估计 + dirty segment 增量的累计

        for i, seg in enumerate(segments):
            if i < session.split_index:
                continue

            if not seg.created:
                estimated = estimate_segment_elements(seg, all_steps)
                if (
                    seg.type == SegmentType.TOOL
                    and session.element_count + new_el_total + estimated + FOOTER_RESERVE > ELEMENT_THRESHOLD
                    and not session.split_disabled
                ):
                    split_offset = find_tool_split_offset(
                        base_count=session.element_count + new_el_total,
                        seg=seg,
                        all_steps=all_steps,
                    )
                    if split_offset is not None:
                        segment_state.split_tool_segment(i, split_offset)
                        estimated = estimate_segment_elements(seg, all_steps)
                if (
                    session.element_count + new_el_total + estimated + FOOTER_RESERVE > ELEMENT_THRESHOLD
                    and session.element_count + new_el_total > 1
                    and not session.split_disabled
                ):
                    split_ok = await self._do_split_card(
                        session, i, actions, new_el_ids, new_el_estimates, updated_tool_segs,
                    )
                    if not split_ok:
                        return
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    updated_tool_segs = []
                    new_el_total = 0

                if seg.type == SegmentType.TOOL:
                    updated_tool_segs.append(seg)
                new_el_ids.add(seg.el_id)
                new_el_estimates[seg.el_id] = estimated
                new_el_total += estimated
                actions.append(build_add_segment_action(seg, all_steps, text_size=self._cfg.body_text_size))
                if (
                    seg.type == SegmentType.TOOL
                    and i + 1 < len(segments)
                    and segments[i + 1].type == SegmentType.TOOL
                    and segments[i + 1].tool_offset == seg.tool_end_offset
                    and not session.split_disabled
                ):
                    split_ok = await self._do_split_card(
                        session, i + 1, actions, new_el_ids, new_el_estimates, updated_tool_segs,
                    )
                    if not split_ok:
                        return
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    updated_tool_segs = []
                    new_el_total = 0
            elif seg.type == SegmentType.REASONING and seg.elapsed_ms > 0 and not seg.reasoning_finalized:
                _logger.info(
                    "CardKit reasoning finalized: msg=%s el=%s elapsed=%.0fms seq=%d",
                    session.message_id[:12],
                    seg.el_id,
                    seg.elapsed_ms,
                    session.sequence + 1,
                )
                actions.append(build_reasoning_finalized_action(seg))
            elif seg.type == SegmentType.TOOL and seg.dirty:
                if seg.tool_end_offset > 0:
                    start, end = seg.tool_offset, seg.tool_end_offset
                else:
                    start, end = seg.tool_offset, len(all_steps)
                rollover = await self._maybe_rollover_tool_segment(
                    session=session,
                    segment_state=segment_state,
                    index=i,
                    seg=seg,
                    all_steps=all_steps,
                    actions=actions,
                    new_el_ids=new_el_ids,
                    new_el_estimates=new_el_estimates,
                    updated_tool_segs=updated_tool_segs,
                    pending_delta=new_el_total,
                )
                if rollover == "failed":
                    return
                if rollover == "split":
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    updated_tool_segs = []
                    new_el_total = 0
                    continue
                estimate = estimate_tool_elements(start, end, all_steps)
                actions.append(
                    build_tool_update_action(element_id=seg.el_id, steps=all_steps[start:end])
                )
                updated_tool_segs.append(seg)
                new_el_estimates[seg.el_id] = estimate
                new_el_total += estimate - seg.element_estimate

        if actions and not await self._do_batch_update(
            session, segments, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        ):
            return

        # ── 步骤 2: stream_element 刷脏文本 ──
        for seg in segments[session.split_index:]:
            if not seg.created or not seg.dirty:
                continue
            try:
                if seg.type == SegmentType.REASONING:
                    content = optimize_markdown_style(seg.text) or " "
                    session.sequence += 1
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=reasoning len=%d",
                        session.message_id[:12],
                        session.sequence,
                        len(content),
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.text_el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
                elif seg.type == SegmentType.ANSWER:
                    content = seg.text
                    if session.image_resolver:
                        content = session.image_resolver.resolve_images(content)
                    content = _downgrade_tables(optimize_markdown_style(content)) or " "
                    session.sequence += 1
                    _logger.info(
                        "CardKit stream element: msg=%s seq=%d type=answer len=%d",
                        session.message_id[:12],
                        session.sequence,
                        len(content),
                    )
                    await self._client.cardkit_stream_element(
                        session.card_id,
                        seg.el_id,
                        content,
                        sequence=session.sequence,
                    )
                    seg.dirty = False
            except Exception as e:
                _logger.debug("CardKit stream element failed: %s el=%s", e, seg.el_id, exc_info=True)

    async def _do_batch_update(
        self,
        session: CardSession,
        segments: list[Segment],
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
    ) -> bool:
        """执行 batch_update 并处理快照/标记。返回 False 表示失败."""
        assert self._client is not None
        assert session.card_id is not None
        session.sequence += 1
        _logger.info(
            "CardKit batch update: msg=%s card=%s seq=%d actions=%d split=%d elements=%d",
            session.message_id[:12],
            session.card_id[:12],
            session.sequence,
            len(actions),
            session.split_index,
            session.element_count,
        )
        pre_flush_reasoning_elapsed = {
            seg.el_id: seg.elapsed_ms for seg in segments if seg.type == SegmentType.REASONING
        }
        pre_flush_tool_offsets = {
            seg.el_id: seg.tool_end_offset for seg in updated_tool_segs
        }
        pre_flush_tool_steps = session.tool_use.build_display_steps()
        pre_flush_tool_slices = {
            seg.el_id: pre_flush_tool_steps[seg.tool_offset:tool_segment_end(seg, pre_flush_tool_steps)]
            for seg in updated_tool_segs
        }
        try:
            await self._client.cardkit_batch_update(
                session.card_id,
                actions,
                sequence=session.sequence,
            )
            for seg in segments:
                if seg.el_id in new_el_ids:
                    seg.created = True
                    estimate = new_el_estimates.get(seg.el_id, 0)
                    seg.element_estimate = estimate
                    session.element_count += estimate
            for seg in segments:
                if seg.type == SegmentType.REASONING and pre_flush_reasoning_elapsed.get(seg.el_id, 0) > 0:
                    seg.reasoning_finalized = True
            if new_el_ids:
                for seg in segments:
                    if seg.el_id in new_el_ids or not seg.created:
                        continue
                    if seg.type in (SegmentType.REASONING, SegmentType.ANSWER) and seg.text:
                        seg.dirty = True
            current_tool_steps = session.tool_use.build_display_steps()
            for seg in updated_tool_segs:
                offset_ok = pre_flush_tool_offsets.get(seg.el_id, -1) == seg.tool_end_offset
                current_tool_slice = current_tool_steps[
                    seg.tool_offset:tool_segment_end(seg, current_tool_steps)
                ]
                tool_slice_ok = pre_flush_tool_slices.get(seg.el_id) == current_tool_slice
                if seg.el_id in new_el_estimates:
                    estimate = new_el_estimates[seg.el_id]
                    session.element_count += estimate - seg.element_estimate
                    seg.element_estimate = estimate
                if seg.created and offset_ok and tool_slice_ok:
                    seg.dirty = False
        except FeishuAPIError as e:
            missing_el_id = extract_missing_element_id(e)
            action_summary = summarize_actions(actions)
            _logger.warning(
                "CardKit batch update failed: %s card=%s seq=%d split=%d elements=%d "
                "missing=%s missing_state=%s new=[%s] tool_updates=[%s] %s",
                e,
                session.card_id[:12],
                session.sequence,
                session.split_index,
                session.element_count,
                missing_el_id or "-",
                segment_state_for_log(segments, missing_el_id),
                compact_ids(new_el_ids),
                compact_ids([seg.el_id for seg in updated_tool_segs]),
                action_summary,
                exc_info=True,
            )
            self._handle_flush_error(e)
            return False
        return True

    async def _maybe_rollover_tool_segment(
        self,
        *,
        session: CardSession,
        segment_state: SegmentState,
        index: int,
        seg: Segment,
        all_steps: list[ToolDisplayStep],
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
        pending_delta: int = 0,
    ) -> str | None:
        """按 tool step 边界拆分过大的 dirty tool segment."""
        start = seg.tool_offset
        end = tool_segment_end(seg, all_steps)
        estimate = estimate_tool_elements(start, end, all_steps)
        delta = estimate - seg.element_estimate
        if (
            delta <= 0
            or session.element_count + pending_delta + delta + FOOTER_RESERVE <= ELEMENT_THRESHOLD
            or session.split_disabled
        ):
            return None

        split_offset = find_tool_split_offset(
            base_count=session.element_count + pending_delta - seg.element_estimate,
            seg=seg,
            all_steps=all_steps,
        )
        if split_offset is None:
            return None

        old_estimate = estimate_tool_elements(seg.tool_offset, split_offset, all_steps)
        actions.append(
            build_tool_update_action(
                element_id=seg.el_id,
                steps=all_steps[seg.tool_offset:split_offset],
            )
        )
        updated_tool_segs.append(seg)
        new_el_estimates[seg.el_id] = old_estimate
        segment_state.split_tool_segment(index, split_offset)
        split_ok = await self._do_split_card(
            session, index + 1, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        )
        if not split_ok:
            return "failed"
        return "split"

    async def _do_split_card(
        self,
        session: CardSession,
        split_idx: int,
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
    ) -> bool:
        """拆卡：先 flush pending actions，封旧卡，创建新卡。返回 False 表示失败需中断 flush."""
        assert self._client is not None
        old_card_id = session.card_id
        assert old_card_id is not None
        segment_state = session.segment_state
        assert segment_state is not None
        segments = segment_state.segments
        all_steps = session.tool_use.build_display_steps()
        seal_start_idx = session.split_index

        if actions and not await self._do_batch_update(
            session, segments, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        ):
            return False

        seal_segments = [s for s in segments[seal_start_idx:split_idx] if s.created]
        if session.image_resolver:
            await _resolve_answer_images(
                seal_segments,
                session.image_resolver,
                log_prefix="CardKit seal",
            )

        seal_card = build_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
            footer_enabled=False,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=False,
            body_text_size=self._cfg.body_text_size,
        )

        try:
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
                header_enabled=self._cfg.header_enabled,
                text_size=self._cfg.body_text_size,
            )
            new_card_id = await self._client.cardkit_create(card)
            new_msg_id = await self._client.reply_card_by_id(session.anchor_id or session.message_id, new_card_id)
        except Exception:
            _logger.warning(
                "CardKit split fallback: create next card failed, continue on current card",
                exc_info=True,
            )
            # 拆卡失败时降级为继续写当前卡，并禁用后续拆卡重试以避免反复卡在同一边界。
            session.split_disabled = True
            return True

        try:
            session.sequence += 1
            await self._client.cardkit_close_streaming(old_card_id, sequence=session.sequence)
            session.sequence += 1
            await self._client.cardkit_update(old_card_id, seal_card, sequence=session.sequence)
        except Exception:
            _logger.warning(
                "CardKit seal failed for old card %s, continuing",
                old_card_id[:12],
                exc_info=True,
            )

        session.set_card(card_id=new_card_id, card_msg_id=new_msg_id)
        session.element_count = 1  # loading
        session.sequence = 1
        session.split_disabled = False
        session.split_index = split_idx
        for seg in segments[split_idx:]:
            seg.created = False
        _logger.info(
            "CardKit split: msg=%s old_card=%s sealed=%d split_idx=%d new_card=%s",
            session.message_id[:12],
            old_card_id[:12],
            len(seal_segments),
            split_idx,
            new_card_id[:12],
        )
        return True

    def _handle_flush_error(self, e: FeishuAPIError) -> None:
        if e.code == CARDKIT_RATE_LIMITED:
            return
        if e.code == CARDKIT_STREAMING_CLOSED:
            return
        if e.code == CARDKIT_CONTENT_FAILED:
            sub_code = e.extract_sub_code()
            if sub_code == CARDKIT_ELEMENT_LIMIT:
                _logger.warning("CardKit card element limit exceeded")

    async def _do_complete_card(self, session: CardSession) -> bool:
        """完成流式卡片：close streaming + 全量重建卡片（保持 segments 顺序）."""
        try:
            return await self._do_complete_card_inner(session)
        finally:
            self._flush_deferred_background_reviews(session)
            self._cleanup(session.message_id)

    async def _do_complete_card_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete_card"):
            return False

        await session.flush.wait_for_flush()
        session.flush.mark_completed()

        segment_state = session.segment_state
        is_error = session.state == SessionState.FAILED
        is_aborted = session.state == SessionState.ABORTED
        all_tool_steps = session.tool_use.build_display_steps()

        if segment_state is not None:
            segment_state.finalize_segments(len(all_tool_steps))

        active_segments = session.active_segments()

        if session.image_resolver:
            await _resolve_answer_images(
                active_segments,
                session.image_resolver,
                log_prefix="CardKit",
            )

        card = build_complete_card(
            segments=active_segments,
            all_tool_steps=all_tool_steps,
            footer_data=session.footer,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
            footer_enabled=self._cfg.footer_enabled,
            footer_text_size=self._cfg.footer_text_size,
            panel_expanded=self._cfg.panel_expanded,
            header_enabled=self._cfg.header_enabled,
            body_text_size=self._cfg.body_text_size,
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
                session.state = SessionState.COMPLETED
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "CardKit complete attempt %d failed: code=%s msg=%s card_id=%s seq=%d",
                    attempt,
                    e.code,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if session.guard.terminate("_do_complete_card", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "CardKit complete attempt %d failed: %s: %s card_id=%s seq=%d",
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
            "CardKit complete failed after 3 attempts: card_id=%s seq=%d",
            session.card_id,
            session.sequence,
        )
        session.mark_failed()
        return False

    async def _do_cron_deliver(
        self, chat_id: str, content: str, *, task_name: str = "", run_time: str = ""
    ) -> None:
        await self._ensure_init()
        assert self._client is not None
        card = build_cron_card(content, task_name=task_name, run_time=run_time)
        await self._client.send_card_to_chat(chat_id, card)

    async def _do_background_deliver(
        self,
        chat_id: str,
        preview: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        await self._ensure_init()
        assert self._client is not None
        card = build_background_card(preview, content)
        await self._client.send_card_to_chat(
            chat_id,
            card,
            reply_to_message_id=reply_to_message_id,
        )
