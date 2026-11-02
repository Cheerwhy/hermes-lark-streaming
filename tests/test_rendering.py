"""保留原有 CardKit 创建、拆卡和收尾回归用例."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hermes_lark_streaming.config import Config
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.streaming.segment_helper import estimate_segment_elements
from hermes_lark_streaming.streaming.segments import Segment
from hermes_lark_streaming.streaming.session import CardSession, SessionState


def _make_session(msg_id="msg_123"):
    session = CardSession(msg_id, "chat_456", asyncio.get_running_loop())
    session.anchor_id = msg_id
    return session


def _setup_ctrl():
    cfg = Config(extra={"card": {"enabled": True}})
    client = AsyncMock(spec=FeishuClient)
    client.cardkit_create.return_value = "card_id_abc"
    client.reply_card_by_id.return_value = "msg_id_reply"
    return StreamCardController(cfg, client)


def _capture_split_calls(
    ctrl: StreamCardController,
    *,
    cards: list[str] | None = None,
    messages: list[str] | None = None,
    create_error: Exception | None = None,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    client = ctrl._client
    card_iter = iter(cards or ["card_next"])
    message_iter = iter(messages or ["msg_next"])

    client.cardkit_batch_update = AsyncMock(side_effect=lambda card_id, *a, **k: calls.append(("batch", card_id)))
    if create_error is None:
        client.cardkit_create = AsyncMock(side_effect=lambda *a, **k: calls.append(("create", "")) or next(card_iter))
    else:
        client.cardkit_create = AsyncMock(side_effect=create_error)
    client.reply_card_by_id = AsyncMock(side_effect=lambda *a, **k: calls.append(("reply", "")) or next(message_iter))
    client.cardkit_close_streaming = AsyncMock(side_effect=lambda card_id, **k: calls.append(("close", card_id)))
    client.cardkit_update = AsyncMock(side_effect=lambda card_id, *a, **k: calls.append(("seal", card_id)))
    return calls


# ── 卡片创建 ──


class TestDoCreateCard:
    @pytest.mark.asyncio
    async def test_cardkit_success(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_create")

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.card_id == "card_id_abc"
        assert session.state == SessionState.STREAMING

    @pytest.mark.asyncio
    async def test_applies_width_mode_to_streaming_card(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._extra["card"]["width_mode"] = "compact"
        session = _make_session("msg_width_create")

        await ctrl._do_create_card(session)

        card = ctrl._client.cardkit_create.await_args.args[0]
        assert card["config"]["width_mode"] == "compact"

    @pytest.mark.asyncio
    async def test_content_failure_recreates_card_and_retries_reply(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(
            side_effect=[FeishuAPIError("invalid card", code=230099), "reply_second"]
        )
        session = _make_session("msg_retry")

        await ctrl._do_create_card(session)

        assert session.card_id == "card_second"
        assert session.card_msg_id == "reply_second"
        assert ctrl._client.cardkit_create.await_count == 2
        assert ctrl._client.reply_card_by_id.await_count == 2

    @pytest.mark.asyncio
    async def test_content_failure_uses_standalone_card_after_retry_fails(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(side_effect=FeishuAPIError("invalid card", code=230099))
        ctrl._client.send_card_to_chat = AsyncMock(return_value="standalone_reply")
        session = _make_session("msg_standalone")

        await ctrl._do_create_card(session)

        assert session.card_id == "card_second"
        assert session.card_msg_id == "standalone_reply"
        ctrl._client.send_card_to_chat.assert_awaited_once_with(
            chat_id="chat_456",
            card={"type": "card", "data": {"card_id": "card_second"}},
        )

    @pytest.mark.asyncio
    async def test_standalone_card_completion_does_not_require_text_fallback(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_create = AsyncMock(side_effect=["card_first", "card_second"])
        ctrl._client.reply_card_by_id = AsyncMock(side_effect=FeishuAPIError("invalid card", code=230099))
        ctrl._client.send_card_to_chat = AsyncMock(return_value="standalone_reply")
        session = _make_session("msg_standalone_complete")

        await ctrl._do_create_card(session)

        session.segment_state.on_answer_delta("answer")
        assert await ctrl._do_complete_card(session) is True
        ctrl._client.cardkit_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cardkit_failure_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_create = AsyncMock(side_effect=FeishuAPIError("fail", code=230099))
        session = _make_session("msg_fallback")

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.state == SessionState.FAILED
        assert not session.has_card

    @pytest.mark.asyncio
    async def test_generic_failure_marks_failed(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client = None
        session = _make_session("msg_err")

        await ctrl._do_create_card(session)

        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_segment_state_set_before_await(self) -> None:
        """首个网络请求前已准备好会话状态."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_early")

        async def create_card(card: dict) -> str:
            assert session.state == SessionState.CREATING
            assert session.segment_state is not None
            return "card_id_abc"

        ctrl._client.cardkit_create.side_effect = create_card
        await ctrl._do_create_card(session)

    @pytest.mark.asyncio
    async def test_post_create_flush_on_dirty(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_dirty")

        async def create_card(card: dict) -> str:
            session.segment_state.on_reasoning_delta("during-creating")
            return "card_id_abc"

        ctrl._client.cardkit_create.side_effect = create_card

        with patch.object(ctrl, "_schedule_flush") as m:
            await ctrl._do_create_card(session)
            m.assert_called()


# ── _do_flush 集成测试 ──


class TestDoFlush:
    @pytest.mark.asyncio
    async def test_three_step_pipeline(self) -> None:
        """step1 创建元素 → step2 刷文本 → step3 创建 tool 面板."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_flush")
        session.state = SessionState.STREAMING
        session.card_id = "card_flush"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("hello world")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)

        await ctrl._do_flush(session)

        # step1: elements created
        assert session.segment_state.segments[0].created is True
        assert session.segment_state.segments[1].created is True
        # step2: dirty cleared for reasoning + answer
        assert session.segment_state.segments[0].dirty is False
        assert session.segment_state.segments[1].dirty is False
        # step2: stream_element called with answer text
        ctrl._client.cardkit_stream_element.assert_called()
        assert "hello world" in ctrl._client.cardkit_stream_element.call_args[0][2]
        # step3: tool created
        tool_seg = session.segment_state.segments[2]
        assert tool_seg.created is True

    @pytest.mark.asyncio
    async def test_no_split_keeps_original_single_card_flow(self) -> None:
        """低于阈值时仍是原来的单卡 flush：只 batch/stream 当前 card，不触发拆卡 API."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_no_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_no_split"
        session.element_count = 1
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("hello")

        await ctrl._do_flush(session)

        assert session.split_index == 0
        assert session.card_id == "card_no_split"
        assert [s.created for s in session.segment_state.segments] == [True, True]
        assert [s.dirty for s in session.segment_state.segments] == [False, False]
        ctrl._client.cardkit_create.assert_not_called()
        ctrl._client.reply_card_by_id.assert_not_called()
        ctrl._client.cardkit_close_streaming.assert_not_called()
        ctrl._client.cardkit_update.assert_not_called()
        ctrl._client.cardkit_batch_update.assert_called_once()
        assert ctrl._client.cardkit_stream_element.call_count == 2

    @pytest.mark.asyncio
    async def test_split_flushes_pending_actions_then_moves_to_next_card(self) -> None:
        """超阈值时先把 pending segment 写入旧卡，再封旧卡并把后续 segment 写入新卡."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(ctrl)

        session = _make_session("msg_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_old"
        session.card_msg_id = "msg_old"
        session.element_count = 174
        session.segment_state.on_reasoning_delta("old")
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].dirty = False
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file")
        session.segment_state.on_tool_event(1)

        await ctrl._do_flush(session)

        assert calls == [
            ("batch", "card_old"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_old"),
            ("seal", "card_old"),
            ("batch", "card_next"),
        ]
        assert session.card_id == "card_next"
        assert session.card_msg_id == "msg_next"
        assert session.split_index == 2
        assert session.split_disabled is False
        assert session.element_count > 1
        assert [s.created for s in session.segment_state.segments] == [True, True, True]

    @pytest.mark.asyncio
    async def test_second_split_seals_only_current_card_segments(self) -> None:
        """多次拆卡时，seal 不应重复包含更早卡片上的 segments."""
        ctrl = _setup_ctrl()
        ctrl._cfg._extra["card"]["width_mode"] = "compact"
        sealed_cards: list[dict] = []
        ctrl._client.cardkit_create = AsyncMock(return_value="card_page_3")
        ctrl._client.reply_card_by_id = AsyncMock(return_value="msg_page_3")
        ctrl._client.cardkit_update = AsyncMock(side_effect=lambda _card_id, card, **_kwargs: sealed_cards.append(card))

        session = _make_session("msg_second_split")
        session.state = SessionState.STREAMING
        session.card_id = "card_page_2"
        session.card_msg_id = "msg_page_2"
        session.split_index = 2
        for index in range(5):
            seg = Segment("answer", f"answer_{index}")
            seg.text = f"page content {index}"
            seg.created = True
            seg.dirty = False
            session.segment_state.segments.append(seg)

        assert await ctrl._do_split_card(session, 5, [], set(), {}, []) is True

        contents = [element["content"] for element in sealed_cards[0]["body"]["elements"]]
        assert contents == ["page content 2", "page content 3", "page content 4"]
        assert sealed_cards[0]["config"]["width_mode"] == "compact"
        next_card = ctrl._client.cardkit_create.await_args.args[0]
        assert next_card["config"]["width_mode"] == "compact"
        assert session.split_index == 5

    @pytest.mark.asyncio
    async def test_tool_growth_rolls_over_at_step_boundary(self) -> None:
        """同一个 tool segment 增长超阈值时，在 step 边界拆到新卡继续更新."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(
            ctrl,
            cards=["card_tool_next"],
            messages=["msg_tool_next"],
        )

        session = _make_session("msg_tool_roll")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_old"
        session.card_msg_id = "msg_tool_old"
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = 174

        for idx in range(1, 4):
            session.tool_use.record_start("read", f"file{idx}")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        assert calls == [
            ("batch", "card_tool_old"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_tool_old"),
            ("seal", "card_tool_old"),
            ("batch", "card_tool_next"),
        ]
        assert session.card_id == "card_tool_next"
        assert session.split_index == 1
        assert len(session.segment_state.segments) == 2
        assert session.segment_state.segments[0].tool_end_offset == 1
        assert session.segment_state.segments[1].tool_offset == 1
        assert session.segment_state.segments[1].created is True

    @pytest.mark.asyncio
    async def test_tool_growth_counts_pending_new_segments_before_rollover(self) -> None:
        """同一轮 flush 内，dirty tool 拆分判断要计入前面尚未落账的新 segment."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(
            ctrl,
            cards=["card_tool_pending_next"],
            messages=["msg_tool_pending_next"],
        )

        session = _make_session("msg_tool_pending_roll")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_pending_old"
        session.card_msg_id = "msg_tool_pending_old"
        session.element_count = 175
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[1]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())

        session.tool_use.record_start("read", "file1")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        assert calls == [
            ("batch", "card_tool_pending_old"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_tool_pending_old"),
            ("seal", "card_tool_pending_old"),
            ("batch", "card_tool_pending_next"),
        ]
        assert session.card_id == "card_tool_pending_next"
        assert session.split_index == 2
        assert len(session.segment_state.segments) == 3
        assert session.segment_state.segments[1].tool_end_offset == 1
        assert session.segment_state.segments[2].tool_offset == 1
        assert session.segment_state.segments[2].created is True

    @pytest.mark.asyncio
    async def test_oversized_new_tool_segment_splits_across_multiple_cards(self) -> None:
        """单次 flush 内 tool steps 很多时，未创建的 tool segment 也会连续分片拆卡."""
        ctrl = _setup_ctrl()
        calls = _capture_split_calls(
            ctrl,
            cards=["card_tool_page_2", "card_tool_page_3"],
            messages=["msg_tool_page_2", "msg_tool_page_3"],
        )

        session = _make_session("msg_tool_many")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_page_1"
        session.card_msg_id = "msg_tool_page_1"
        session.element_count = 1
        session.tool_use.record_start("check")
        session.segment_state.on_tool_event(1)
        for _ in range(127):
            session.tool_use.record_start("check")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        assert calls == [
            ("batch", "card_tool_page_1"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_tool_page_1"),
            ("seal", "card_tool_page_1"),
            ("batch", "card_tool_page_2"),
            ("create", ""),
            ("reply", ""),
            ("close", "card_tool_page_2"),
            ("seal", "card_tool_page_2"),
            ("batch", "card_tool_page_3"),
        ]
        assert session.card_id == "card_tool_page_3"
        assert session.card_msg_id == "msg_tool_page_3"
        assert session.split_index == 2
        assert len(session.segment_state.segments) == 3
        assert [s.tool_offset for s in session.segment_state.segments] == [0, 58, 116]
        assert [s.tool_end_offset for s in session.segment_state.segments] == [58, 116, 0]
        assert all(s.created for s in session.segment_state.segments)
        assert session.segment_state.segments[-1].element_estimate + session.element_count <= 180

    @pytest.mark.asyncio
    async def test_tool_rollover_create_failure_falls_back_on_current_card(self) -> None:
        """tool rollover 新卡创建失败后，在当前卡保留 step 分界并禁用后续拆卡重试."""
        ctrl = _setup_ctrl()
        batch_card_ids = _capture_split_calls(ctrl, create_error=RuntimeError("create failed"))
        client = ctrl._client

        session = _make_session("msg_tool_roll_fallback")
        session.state = SessionState.STREAMING
        session.card_id = "card_tool_current"
        session.card_msg_id = "msg_tool_current"
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = 174

        for idx in range(1, 4):
            session.tool_use.record_start("read", f"file{idx}")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        assert session.card_id == "card_tool_current"
        assert session.split_index == 0
        assert session.split_disabled is True
        assert len(session.segment_state.segments) == 2
        assert session.segment_state.segments[0].tool_end_offset == 1
        assert session.segment_state.segments[1].tool_offset == 1
        assert session.segment_state.segments[1].created is True
        assert batch_card_ids == [("batch", "card_tool_current"), ("batch", "card_tool_current")]
        client.cardkit_close_streaming.assert_not_called()
        client.cardkit_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_split_create_failure_falls_back_to_current_card(self) -> None:
        """新卡创建失败是有意降级：不推进 split_index，继续把后续内容写回当前卡."""
        ctrl = _setup_ctrl()
        batch_card_ids = _capture_split_calls(ctrl, create_error=RuntimeError("create failed"))
        client = ctrl._client

        session = _make_session("msg_split_fallback")
        session.state = SessionState.STREAMING
        session.card_id = "card_current"
        session.card_msg_id = "msg_current"
        session.element_count = 174
        session.segment_state.on_reasoning_delta("old")
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].dirty = False
        session.segment_state.on_answer_delta("pending answer")
        session.tool_use.record_start("read", "file")
        session.segment_state.on_tool_event(1)

        await ctrl._do_flush(session)

        assert session.card_id == "card_current"
        assert session.card_msg_id == "msg_current"
        assert session.split_index == 0
        assert session.split_disabled is True
        assert session.element_count > 174
        assert batch_card_ids == [("batch", "card_current"), ("batch", "card_current")]
        assert session.segment_state.segments[2].created is True
        client.cardkit_close_streaming.assert_not_called()
        client.cardkit_update.assert_not_called()

        client.cardkit_create.reset_mock()
        session.segment_state.on_answer_delta(" after fallback")

        await ctrl._do_flush(session)

        client.cardkit_create.assert_not_called()
        assert batch_card_ids == [
            ("batch", "card_current"),
            ("batch", "card_current"),
            ("batch", "card_current"),
        ]
        assert session.segment_state.segments[-1].created is True

    @pytest.mark.asyncio
    async def test_batch_update_recovers_stale_segment_on_missing_element(self) -> None:
        """300313 (not find elementID) 后回滚 stale segment，下一轮 flush 走 add 重建，不再死循环."""
        ctrl = _setup_ctrl()
        client = ctrl._client

        session = _make_session("msg_stale")
        session.state = SessionState.STREAMING
        session.card_id = "card_stale"
        session.card_msg_id = "msg_stale_card"
        # 一个已创建的 tool segment（本地 created=True，但模拟卡片上已不存在）
        session.tool_use.record_start("read", "file0")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True
        tool_seg.element_estimate = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        session.element_count = tool_seg.element_estimate

        stale_el_id = tool_seg.el_id
        # 第一次 batch_update 抛 300313 — 引用了卡片上不存在的 el_id
        client.cardkit_batch_update = AsyncMock(
            side_effect=[
                FeishuAPIError(
                    f"cardkit_batch_update: code=300313, msg=ErrMsg: not find elementID : {stale_el_id};",
                    300313,
                ),
                None,
            ]
        )

        # 新增 tool step 触发 dirty → flush 走 partial_update 分支 → 300313
        session.tool_use.record_start("read", "file1")
        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))

        await ctrl._do_flush(session)

        # 回滚：stale segment 被标记为未创建 + 脏，等下一轮重建
        assert tool_seg.created is False
        assert tool_seg.dirty is True
        # element_count 同步扣减（避免下一轮 add 重复累加导致阈值虚高、误触发拆分）
        assert session.element_count == 0

        # 第二次 flush：走 add_elements 重建，batch_update 成功
        await ctrl._do_flush(session)

        assert tool_seg.created is True
        # 重建后 element_count = 当前 tool steps（2 个）的新估算值，无重复计数
        expected = estimate_segment_elements(tool_seg, session.tool_use.build_display_steps())
        assert session.element_count == expected
        # 两次 batch_update 调用：第一次失败，第二次成功（非死循环重试 N 次）
        assert client.cardkit_batch_update.await_count == 2

    @pytest.mark.asyncio
    async def test_reasoning_finalized_snapshot(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_snap"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("reply")
        session.segment_state.segments[0].elapsed_ms = 1500.0
        session.segment_state.segments[0].reasoning_finalized = False

        await ctrl._do_flush(session)

        assert session.segment_state.segments[0].reasoning_finalized is True

    @pytest.mark.asyncio
    async def test_reasoning_title_update_with_elapsed(self) -> None:
        ctrl = _setup_ctrl()
        batch_calls: list[list[dict]] = []

        async def capture_batch(card_id: str, actions: list[dict], **kw: object) -> None:
            batch_calls.append(actions)

        ctrl._client.cardkit_batch_update = capture_batch

        session = _make_session("msg_title")
        session.state = SessionState.STREAMING
        session.card_id = "card_title"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("reply")
        session.segment_state.segments[0].elapsed_ms = 2500.0
        session.segment_state.segments[0].created = True
        session.segment_state.segments[0].reasoning_finalized = False

        await ctrl._do_flush(session)

        partials = [a for a in batch_calls[0] if a["action"] == "partial_update_element"]
        assert len(partials) == 1
        assert "2.5s" in partials[0]["params"]["partial_element"]["header"]["title"]["content"]

    @pytest.mark.asyncio
    async def test_tool_dirty_snapshot(self) -> None:
        """await 期间 tool_end_offset 变化 → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        tool_seg_ref: Segment | None = None
        batch_counter = 0

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            nonlocal batch_counter
            await original_batch(card_id, actions, **kw)
            batch_counter += 1
            if batch_counter == 1 and tool_seg_ref is not None and tool_seg_ref.tool_end_offset == 0:
                tool_seg_ref.tool_end_offset = 5

        ctrl._client.cardkit_batch_update = batch_with_race

        session = _make_session("msg_tool_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_snap"
        session.segment_state.on_answer_delta("text")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg_ref = session.segment_state.segments[1]

        await ctrl._do_flush(session)

        assert tool_seg_ref.tool_end_offset == 5
        assert tool_seg_ref.dirty is True

    @pytest.mark.asyncio
    async def test_open_tool_dirty_cleared_when_steps_unchanged(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_open_tool_clean")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_clean"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is False

    @pytest.mark.asyncio
    async def test_open_tool_dirty_snapshot_when_steps_change(self) -> None:
        """await 期间 open tool 新增 step → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        session = _make_session("msg_open_tool_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_snap"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            await original_batch(card_id, actions, **kw)
            session.tool_use.record_start("grep", "q")
            session.segment_state.on_tool_event(2)

        ctrl._client.cardkit_batch_update = batch_with_race

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is True

    @pytest.mark.asyncio
    async def test_open_tool_dirty_snapshot_when_step_content_changes(self) -> None:
        """await 期间 open tool 完成但 step 数不变 → dirty 保持."""
        ctrl = _setup_ctrl()
        original_batch = ctrl._client.cardkit_batch_update
        session = _make_session("msg_open_tool_content_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_open_tool_content_snap"
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)
        tool_seg = session.segment_state.segments[0]
        tool_seg.created = True

        async def batch_with_race(card_id: str, actions: list[dict], **kw: object) -> None:
            await original_batch(card_id, actions, **kw)
            session.tool_use.record_end("read", output="done")
            session.segment_state.on_tool_event(1)

        ctrl._client.cardkit_batch_update = batch_with_race

        await ctrl._do_flush(session)

        assert tool_seg.tool_end_offset == 0
        assert tool_seg.dirty is True

    @pytest.mark.asyncio
    async def test_step2_exception_does_not_block_step3(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_stream_element = AsyncMock(side_effect=RuntimeError("stream fail"))
        session = _make_session("msg_exc")
        session.state = SessionState.STREAMING
        session.card_id = "card_exc"
        session.segment_state.on_answer_delta("text")
        session.tool_use.record_start("read", "f")
        session.segment_state.on_tool_event(1)

        await ctrl._do_flush(session)

        assert ctrl._client.cardkit_batch_update.call_count >= 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [230020, 300309])
    async def test_api_errors_swallowed(self, code: int) -> None:
        """rate limited / streaming closed 不抛异常."""
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_batch_update = AsyncMock(side_effect=FeishuAPIError("e", code=code))
        session = _make_session("msg_err")
        session.state = SessionState.STREAMING
        session.card_id = "card_e"
        session.segment_state.on_reasoning_delta("think")

        await ctrl._do_flush(session)

    @pytest.mark.asyncio
    async def test_skip_conditions(self) -> None:
        """终态 / 无 card_id / 无 dirty 全部跳过 API 调用."""
        ctrl = _setup_ctrl()

        # 终态
        s1 = _make_session("m1")
        s1.state = SessionState.COMPLETED
        await ctrl._do_flush(s1)

        # 无 card_id
        s2 = _make_session("m2")
        s2.state = SessionState.STREAMING
        s2.card_id = None
        await ctrl._do_flush(s2)

        # 无 dirty
        s3 = _make_session("m3")
        s3.state = SessionState.STREAMING
        s3.card_id = "c"
        s3.segment_state.on_reasoning_delta("t")
        s3.segment_state.segments[0].created = True
        s3.segment_state.segments[0].dirty = False
        await ctrl._do_flush(s3)

        ctrl._client.cardkit_batch_update.assert_not_called()
        ctrl._client.cardkit_stream_element.assert_not_called()


# ── _do_complete_card 集成测试 ──


class TestDoCompleteCard:
    @pytest.mark.asyncio
    async def test_closes_streaming_then_updates(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._extra["card"]["width_mode"] = "compact"
        call_order: list[str] = []
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock(side_effect=lambda *a, **k: call_order.append("close"))
        client.cardkit_update = AsyncMock(side_effect=lambda *a, **k: call_order.append("update"))

        session = _make_session("msg_comp")
        session.state = SessionState.STREAMING
        session.card_id = "card_comp"
        session.card_msg_id = "msg_comp_reply"

        assert await ctrl._do_complete_card(session) is True
        assert session.state == SessionState.COMPLETED
        assert call_order == ["close", "update"]
        card = client.cardkit_update.await_args.args[1]
        assert card["config"]["width_mode"] == "compact"

    @pytest.mark.asyncio
    async def test_streaming_closed_flag_prevents_double_close(self) -> None:
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock()
        call_count = 0
        original_update = client.cardkit_update

        async def flaky_update(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FeishuAPIError("conflict", code=300317)
            return await original_update(*args, **kwargs)

        client.cardkit_update = flaky_update

        session = _make_session("msg_retry")
        session.state = SessionState.STREAMING
        session.card_id = "card_retry"
        session.card_msg_id = "msg_retry_reply"

        assert await ctrl._do_complete_card(session) is True
        assert client.cardkit_close_streaming.call_count == 1
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_three_retries_exhausted(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client.cardkit_close_streaming = AsyncMock(side_effect=FeishuAPIError("fail", code=99999))

        session = _make_session("msg_3fail")
        session.state = SessionState.STREAMING
        session.card_id = "card_3fail"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await ctrl._do_complete_card(session) is False
        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_finalize_and_cleanup(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_fc")
        session.state = SessionState.STREAMING
        session.card_id = "card_fc"
        session.segment_state.on_reasoning_delta("think")
        await asyncio.sleep(0.001)

        await ctrl._do_complete_card(session)

        assert session.segment_state.segments[0].elapsed_ms > 0
        assert session.flush._completed

    @pytest.mark.asyncio
    async def test_no_card_id_skips_close(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_nocard")
        session.state = SessionState.STREAMING
        session.card_id = None

        assert await ctrl._do_complete_card(session) is True
        assert session.state == SessionState.COMPLETED
        ctrl._client.cardkit_close_streaming.assert_not_called()

    @pytest.mark.asyncio
    async def test_image_resolve_per_segment(self) -> None:
        """单个 segment resolve 失败不影响后续."""
        from unittest.mock import MagicMock

        ctrl = _setup_ctrl()
        session = _make_session("msg_img")
        session.state = SessionState.STREAMING
        session.card_id = "card_img"
        session.segment_state.on_answer_delta("![a](http://x.com/img.png)")
        session.segment_state.on_reasoning_delta("mid")
        session.segment_state.on_answer_delta("![b](http://y.com/img2.png)")

        resolver = MagicMock()
        resolver.resolve_await = AsyncMock(side_effect=[RuntimeError("timeout"), "ok"])
        session.image_resolver = resolver

        await ctrl._do_complete_card(session)

        assert resolver.resolve_await.call_count == 2


# ── _on_thinking_segment 集成测试 ──


@pytest.mark.asyncio
async def test_image_upload_refreshes_previously_flushed_answer() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("image-refresh")
    await ctrl._do_create_card(session)
    session.segment_state.on_answer_delta("![image](https://example.com/image.png)")
    uploaded = asyncio.Event()

    async def upload(url: str) -> str:
        await uploaded.wait()
        return "img_uploaded"

    ctrl._client.upload_image.side_effect = upload
    await ctrl._do_flush(session)
    assert not session.segment_state.segments[0].dirty
    uploaded.set()
    await asyncio.gather(*list(session.image_resolver._pending.values()))
    assert session.segment_state.segments[0].dirty
    await session.flush.flush_now(lambda: ctrl._do_flush(session))
    assert "img_uploaded" in ctrl._client.cardkit_stream_element.call_args.args[2]
    ctrl._cleanup_session(session)


@pytest.mark.asyncio
async def test_new_tool_panel_does_not_restream_unchanged_answer() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("tool-boundary")
    await ctrl._do_create_card(session)
    session.segment_state.on_answer_delta("existing answer")
    await ctrl._do_flush(session)
    calls = ctrl._client.cardkit_stream_element.await_count
    session.tool_use.record_start("terminal", "pwd")
    session.segment_state.on_tool_event(1)
    await ctrl._do_flush(session)
    assert ctrl._client.cardkit_stream_element.await_count == calls
    ctrl._cleanup_session(session)
