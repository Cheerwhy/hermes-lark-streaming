"""线性单卡模式的异步 API 编排 — 创建、刷新、拆卡、完成."""

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

_ELEMENT_THRESHOLD = 180  # 拆卡阈值（飞书硬上限 200，预留 20 给 footer + 波动）
_FOOTER_RESERVE = 2  # footer 元素预留（hr + markdown）


def _estimate_segment_elements(seg: Segment, all_steps: list[dict[str, Any]]) -> int:
    """估算单个 segment 新增的卡片元素数."""
    if seg.type == "reasoning":
        return 4  # collapsible_panel + plain_text + standard_icon + markdown
    elif seg.type == "answer":
        return 1
    elif seg.type == "tool":
        return _estimate_tool_elements(
            seg.tool_offset,
            _tool_segment_end(seg, all_steps),
            all_steps,
        )
    return 0


def _tool_segment_end(seg: Segment, all_steps: list[dict[str, Any]]) -> int:
    return seg.tool_end_offset if seg.tool_end_offset else len(all_steps)


def _estimate_tool_elements(start: int, end: int, all_steps: list[dict[str, Any]]) -> int:
    """估算 tool panel 在 [start, end) step 区间内的元素数."""
    steps = all_steps[start:end]
    count = 3  # panel/header 基础元素
    for step in steps:
        count += 3  # title: div + standard_icon + lark_md
        if step.get("detail"):
            count += 2  # detail: div + plain_text
        if step.get("result_block") or step.get("error_block"):
            count += 2  # output: div + lark_md
    return count

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
                session.element_count = 1  # loading element
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
        """线性模式幂等 flush：按 segment 顺序处理结构性变更，超阈值时拆卡."""
        if session.state in _TERMINAL or not session.card_id:
            return
        linear_state = session.linear_state
        if linear_state is None:
            return

        assert self._client is not None
        segments = linear_state.segments
        all_steps = session.tool_use.build_display_steps()

        # ── 步骤 1: batch_update — 按 segment 顺序处理结构性变更 ──
        actions: list[dict[str, Any]] = []
        new_el_ids: set[str] = set()
        new_el_estimates: dict[str, int] = {}
        updated_tool_segs: list[Segment] = []
        new_el_total = 0

        for i, seg in enumerate(segments):
            if i < session.split_index:
                continue

            if not seg.created:
                estimated = _estimate_segment_elements(seg, all_steps)
                if (
                    seg.type == "tool"
                    and session.element_count + new_el_total + estimated + _FOOTER_RESERVE > _ELEMENT_THRESHOLD
                    and not session.split_disabled
                ):
                    split_offset = self._find_tool_split_offset(
                        session.element_count + new_el_total,
                        seg,
                        all_steps,
                    )
                    if split_offset is not None:
                        linear_state.split_tool_segment(i, split_offset)
                        estimated = _estimate_segment_elements(seg, all_steps)
                if (
                    session.element_count + new_el_total + estimated + _FOOTER_RESERVE > _ELEMENT_THRESHOLD
                    and session.element_count + new_el_total > 1
                    and not session.split_disabled
                ):
                    split_ok = await self._do_linear_split(
                        session, i, actions, new_el_ids, new_el_estimates, updated_tool_segs,
                    )
                    if not split_ok:
                        return
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    updated_tool_segs = []
                    new_el_total = 0

                if seg.type == "reasoning":
                    el = _build_reasoning_panel(
                        " ",
                        seg.elapsed_ms,
                        expanded=True,
                        element_id=seg.el_id,
                        text_element_id=seg.text_el_id,
                    )
                elif seg.type == "answer":
                    el = _streaming_element(element_id=seg.el_id)
                elif seg.type == "tool":
                    start = seg.tool_offset
                    end = seg.tool_end_offset if seg.tool_end_offset else len(all_steps)
                    el = _build_tool_panel(all_steps[start:end], element_id=seg.el_id)
                    updated_tool_segs.append(seg)
                new_el_ids.add(seg.el_id)
                new_el_estimates[seg.el_id] = estimated
                new_el_total += estimated
                actions.append({
                    "action": "add_elements",
                    "params": {
                        "type": "insert_before",
                        "target_element_id": _LOADING_ELEMENT_ID,
                        "elements": [el],
                    },
                })
                if (
                    seg.type == "tool"
                    and i + 1 < len(segments)
                    and segments[i + 1].type == "tool"
                    and segments[i + 1].tool_offset == seg.tool_end_offset
                    and not session.split_disabled
                ):
                    split_ok = await self._do_linear_split(
                        session, i + 1, actions, new_el_ids, new_el_estimates, updated_tool_segs,
                    )
                    if not split_ok:
                        return
                    actions = []
                    new_el_ids = set()
                    new_el_estimates = {}
                    updated_tool_segs = []
                    new_el_total = 0
            elif seg.type == "reasoning" and seg.elapsed_ms > 0 and not seg.reasoning_finalized:
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
                actions.append({
                    "action": "partial_update_element",
                    "params": {
                        "element_id": seg.el_id,
                        "partial_element": {
                            "header": {
                                "title": {
                                    "tag": "plain_text",
                                    "content": f"💭 {en_label}",
                                    "i18n_content": _i18n(f"💭 {en_label}", f"💭 {zh_label}"),
                                    "text_color": "grey",
                                    "text_size": "notation",
                                },
                            },
                        },
                    },
                })
            elif seg.type == "tool" and seg.dirty:
                if seg.tool_end_offset > 0:
                    start, end = seg.tool_offset, seg.tool_end_offset
                else:
                    start, end = seg.tool_offset, len(all_steps)
                rollover = await self._maybe_rollover_tool_segment(
                    session=session,
                    linear_state=linear_state,
                    index=i,
                    seg=seg,
                    all_steps=all_steps,
                    actions=actions,
                    new_el_ids=new_el_ids,
                    new_el_estimates=new_el_estimates,
                    updated_tool_segs=updated_tool_segs,
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
                estimate = _estimate_tool_elements(start, end, all_steps)
                panel = _build_tool_panel(all_steps[start:end])
                actions.append({
                    "action": "partial_update_element",
                    "params": {
                        "element_id": seg.el_id,
                        "partial_element": {
                            "elements": panel["elements"],
                            "header": panel["header"],
                        },
                    },
                })
                updated_tool_segs.append(seg)
                new_el_estimates[seg.el_id] = estimate

        if actions and not await self._do_linear_batch_update(
            session, segments, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        ):
            return

        # ── 步骤 2: stream_element 刷脏文本 ──
        for seg in segments[session.split_index:]:
            if not seg.created or not seg.dirty:
                continue
            try:
                if seg.type == "reasoning":
                    content = optimize_markdown_style(seg.text) or " "
                    session.sequence += 1
                    _logger.info(
                        "linear stream: msg=%s seq=%d type=reasoning len=%d",
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
                elif seg.type == "answer":
                    content = seg.text
                    if session.image_resolver:
                        content = session.image_resolver.resolve_images(content)
                    content = _downgrade_tables(optimize_markdown_style(content)) or " "
                    session.sequence += 1
                    _logger.info(
                        "linear stream: msg=%s seq=%d type=answer len=%d",
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
                _logger.debug("linear stream failed: %s el=%s", e, seg.el_id, exc_info=True)

    async def _do_linear_batch_update(
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
            "linear flush: msg=%s seq=%d actions=%d",
            session.message_id[:12],
            session.sequence,
            len(actions),
        )
        pre_flush_reasoning_elapsed = {
            seg.el_id: seg.elapsed_ms for seg in segments if seg.type == "reasoning"
        }
        pre_flush_tool_offsets = {
            seg.el_id: seg.tool_end_offset for seg in updated_tool_segs
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
                if seg.type == "reasoning" and pre_flush_reasoning_elapsed.get(seg.el_id, 0) > 0:
                    seg.reasoning_finalized = True
            if new_el_ids:
                for seg in segments:
                    if seg.el_id in new_el_ids or not seg.created:
                        continue
                    if seg.type in ("reasoning", "answer") and seg.text:
                        seg.dirty = True
            for seg in updated_tool_segs:
                offset_ok = pre_flush_tool_offsets.get(seg.el_id, -1) == seg.tool_end_offset
                if seg.el_id in new_el_estimates:
                    estimate = new_el_estimates[seg.el_id]
                    session.element_count += estimate - seg.element_estimate
                    seg.element_estimate = estimate
                if seg.created and offset_ok and seg.tool_end_offset > 0:
                    seg.dirty = False
        except FeishuAPIError as e:
            _logger.warning("linear batch update failed: %s", e, exc_info=True)
            self._handle_linear_flush_error(e)
            return False
        return True

    def _find_tool_split_offset(
        self,
        base_count: int,
        seg: Segment,
        all_steps: list[dict[str, Any]],
    ) -> int | None:
        """寻找 tool step 拆分点，让当前卡保留尽可能多的 steps."""
        start = seg.tool_offset
        end = _tool_segment_end(seg, all_steps)
        if end - start <= 1:
            return None
        for split_offset in range(end - 1, start, -1):
            estimate = _estimate_tool_elements(start, split_offset, all_steps)
            if base_count + estimate + _FOOTER_RESERVE <= _ELEMENT_THRESHOLD:
                return split_offset
        return None

    async def _maybe_rollover_tool_segment(
        self,
        *,
        session: CardSession,
        linear_state: LinearState,
        index: int,
        seg: Segment,
        all_steps: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        new_el_ids: set[str],
        new_el_estimates: dict[str, int],
        updated_tool_segs: list[Segment],
    ) -> str | None:
        """按 tool step 边界拆分过大的 dirty tool segment."""
        start = seg.tool_offset
        end = _tool_segment_end(seg, all_steps)
        estimate = _estimate_tool_elements(start, end, all_steps)
        delta = estimate - seg.element_estimate
        if (
            delta <= 0
            or session.element_count + delta + _FOOTER_RESERVE <= _ELEMENT_THRESHOLD
            or session.split_disabled
        ):
            return None

        split_offset = self._find_tool_split_offset(
            session.element_count - seg.element_estimate,
            seg,
            all_steps,
        )
        if split_offset is None:
            return None

        old_estimate = _estimate_tool_elements(seg.tool_offset, split_offset, all_steps)
        panel = _build_tool_panel(all_steps[seg.tool_offset:split_offset])
        actions.append({
            "action": "partial_update_element",
            "params": {
                "element_id": seg.el_id,
                "partial_element": {
                    "elements": panel["elements"],
                    "header": panel["header"],
                },
            },
        })
        updated_tool_segs.append(seg)
        new_el_estimates[seg.el_id] = old_estimate
        linear_state.split_tool_segment(index, split_offset)
        split_ok = await self._do_linear_split(
            session, index + 1, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        )
        if not split_ok:
            return "failed"
        return "split"

    async def _do_linear_split(
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
        linear_state = session.linear_state
        assert linear_state is not None
        segments = linear_state.segments
        all_steps = session.tool_use.build_display_steps()

        if actions and not await self._do_linear_batch_update(
            session, segments, actions, new_el_ids, new_el_estimates, updated_tool_segs,
        ):
            return False

        seal_segments = [s for s in segments[:split_idx] if s.created]
        if session.image_resolver:
            for seg in seal_segments:
                if seg.type == "answer" and seg.text:
                    try:
                        seg.text = await session.image_resolver.resolve_await(seg.text)
                    except Exception:
                        _logger.debug("linear seal image resolve failed: el=%s", seg.el_id, exc_info=True)

        seal_card = build_linear_complete_card(
            segments=seal_segments,
            all_tool_steps=all_steps,
            footer_fields=[],
            footer_show_label=False,
        )

        try:
            card = build_streaming_card_v2(
                show_tool_use=False,
                show_reasoning=False,
                show_streaming_element=False,
            )
            new_card_id = await self._client.cardkit_create(card)
            new_msg_id = await self._client.reply_card_by_id(session.message_id, new_card_id)
        except Exception:
            _logger.warning(
                "linear split fallback: create next card failed, continue on current card",
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
                "linear seal failed for old card %s, continuing",
                old_card_id[:12],
                exc_info=True,
            )

        session.card_id = new_card_id
        session.card_msg_id = new_msg_id
        session.element_count = 1  # loading
        session.sequence = 1
        session.split_disabled = False
        session.split_index = split_idx
        _logger.info(
            "linear split: msg=%s sealed=%d new_card=%s",
            session.message_id[:12],
            len(seal_segments),
            new_card_id[:12],
        )
        return True

    def _handle_linear_flush_error(self, e: FeishuAPIError) -> None:
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

        active_segments = (
            linear_state.segments[session.split_index:] if linear_state is not None else []
        )

        if session.image_resolver:
            for seg in active_segments:
                if seg.type == "answer" and seg.text:
                    try:
                        seg.text = await session.image_resolver.resolve_await(seg.text)
                    except Exception:
                        _logger.debug("linear image resolve failed: el=%s", seg.el_id, exc_info=True)

        card = build_linear_complete_card(
            segments=active_segments,
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
