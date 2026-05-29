"""controller.py 测试 — 会话生命周期边界条件 + 流式卡片 dispatch 与集成测试."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.streaming.segment_helper import estimate_segment_elements
from hermes_lark_streaming.streaming.segments import Segment, SegmentState
from hermes_lark_streaming.streaming.session import CardSession, SessionState


def _enable(ctrl: StreamCardController) -> None:
    ctrl._cfg._raw = {
        "streaming": {"enabled": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }


class _DummyFlush:
    def __init__(self) -> None:
        self.completed = False

    def mark_completed(self) -> None:
        self.completed = True


@pytest.mark.parametrize("message_id", [None, ""])
def test_on_message_started_ignores_missing_message_id(message_id: str | None) -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    ctrl.on_message_started(message_id=message_id, chat_id="chat")

    assert ctrl._sessions == {}


def test_on_message_started_registers_anchor_alias_and_cleanup() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="msg", chat_id="chat", anchor_id="quoted")

    session = ctrl._sessions["msg"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"

    ctrl._cleanup("msg")

    assert "msg" not in ctrl._sessions
    assert "quoted" not in ctrl._sessions


def test_consume_text_fallback_clears_anchor_alias() -> None:
    ctrl = StreamCardController()
    session = _make_session("msg")
    session.anchor_id = "quoted"
    ctrl._sessions["msg"] = session
    ctrl._sessions["quoted"] = session
    ctrl._text_fallback_needed.update({"msg", "quoted"})

    assert ctrl.consume_text_fallback("quoted") is True

    assert "msg" not in ctrl._text_fallback_needed
    assert "quoted" not in ctrl._text_fallback_needed


def test_on_interrupted_uses_new_message_id_and_anchor_alias() -> None:
    ctrl = StreamCardController()
    _enable(ctrl)

    with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
        ctrl.on_message_started(message_id="old", chat_id="chat")
        ctrl.on_interrupted(
            old_message_id="old",
            new_message_id="new",
            chat_id="chat",
            anchor_id="quoted",
        )

    session = ctrl._sessions["new"]
    assert ctrl._sessions["quoted"] is session
    assert session.anchor_id == "quoted"
    assert ctrl._interrupt_map["old"] == "new"
    assert ctrl._sessions["old"].state == SessionState.ABORTED


def test_prune_stale_sessions_ignores_none_key_and_prunes_valid_key() -> None:
    ctrl = StreamCardController()
    stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        image_resolver=None,
    )
    valid_stale_session = SimpleNamespace(
        created_at=time.time() - ctrl._session_ttl - 1,
        flush=_DummyFlush(),
        image_resolver=None,
    )
    ctrl._sessions[None] = stale_session  # type: ignore[index,assignment]
    ctrl._sessions["msg"] = valid_stale_session  # type: ignore[assignment]

    ctrl._prune_stale_sessions()

    assert ctrl._sessions[None] is stale_session  # type: ignore[index]
    assert "msg" not in ctrl._sessions
    assert valid_stale_session.flush.completed


@pytest.mark.asyncio
async def test_background_review_deferred_until_complete() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg_bg")
    session.state = SessionState.STREAMING
    session.card_msg_id = "card_msg"
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    assert ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []

    with patch.object(ctrl, "_do_complete_card_inner", new_callable=AsyncMock, return_value=True):
        await ctrl._do_complete_card(session)

    assert sent == ["review"]
    assert "msg_bg" not in ctrl._sessions


def test_background_review_without_active_session_not_deferred() -> None:
    ctrl = _setup_ctrl()
    sent: list[str] = []

    assert not ctrl.defer_background_review(message_id="missing", text="review", sender=sent.append)
    assert sent == []


def test_background_review_after_flush_not_deferred() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg_bg")
    ctrl._sessions["msg_bg"] = session
    sent: list[str] = []

    ctrl._flush_deferred_background_reviews(session)

    assert not ctrl.defer_background_review(message_id="msg_bg", text="review", sender=sent.append)
    assert sent == []


# ── 辅助函数 ──


def _make_session(msg_id: str = "msg_123") -> CardSession:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return CardSession(msg_id, "chat_456", loop)


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=FeishuClient)
    client.cardkit_create = AsyncMock(return_value="card_id_abc")
    client.reply_card_by_id = AsyncMock(return_value="msg_id_reply")
    client.cardkit_batch_update = AsyncMock()
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    return client


def _setup_ctrl() -> StreamCardController:
    ctrl = StreamCardController()
    _enable(ctrl)
    ctrl._initialized = True
    ctrl._client = _mock_client()
    return ctrl


class TestAwaitedCompletion:
    @pytest.mark.asyncio
    async def test_waits_for_queued_card_creation_before_success(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_wait", "chat", asyncio.get_running_loop())
        ctrl._sessions["msg_wait"] = session
        ready = asyncio.Event()

        async def finish_create() -> None:
            await ready.wait()
            session.card_id = "card_wait"
            session.card_msg_id = "card_msg_wait"
            session.state = SessionState.STREAMING

        session.create_task = asyncio.create_task(finish_create())

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True) as complete:
            waiter = asyncio.create_task(ctrl.on_completed_wait(message_id="msg_wait", answer="ok"))
            await asyncio.sleep(0.01)
            assert not waiter.done()

            ready.set()

            assert await waiter is True
            complete.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_card_creation_timeout_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_timeout", "chat", asyncio.get_running_loop())
        ctrl._sessions["msg_timeout"] = session
        session.create_task = asyncio.create_task(asyncio.sleep(60))

        with patch("hermes_lark_streaming.controller._CARD_CREATION_WAIT_SEC", 0.01):
            assert await ctrl.on_completed_wait(message_id="msg_timeout", answer="ok") is False

        assert session.state == SessionState.FAILED
        assert "msg_timeout" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_finalization_failure_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_fail", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.card_id = "card_fail"
        session.card_msg_id = "card_msg_fail"
        ctrl._sessions["msg_fail"] = session

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=False):
            assert await ctrl.on_completed_wait(message_id="msg_fail", answer="ok") is False

    @pytest.mark.asyncio
    async def test_short_reply_adds_final_answer_segment(self) -> None:
        ctrl = _setup_ctrl()
        session = CardSession("msg_short", "chat", asyncio.get_running_loop())
        session.state = SessionState.STREAMING
        session.segment_state = SegmentState()
        session.card_id = "card_short"
        session.card_msg_id = "card_msg_short"
        ctrl._sessions["msg_short"] = session

        with patch.object(ctrl, "_complete_session_wait", new_callable=AsyncMock, return_value=True):
            assert await ctrl.on_completed_wait(message_id="msg_short", answer="short") is True

        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].type == "answer"
        assert session.segment_state.segments[0].text == "short"


@pytest.mark.asyncio
async def test_create_card_replies_to_anchor_id() -> None:
    ctrl = _setup_ctrl()
    session = _make_session("msg")
    session.anchor_id = "quoted"

    await ctrl._do_create_card(session)

    ctrl._client.reply_card_by_id.assert_called_once()
    assert ctrl._client.reply_card_by_id.call_args.args[0] == "quoted"


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

    client.cardkit_batch_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("batch", card_id))
    )
    if create_error is None:
        client.cardkit_create = AsyncMock(
            side_effect=lambda *a, **k: calls.append(("create", "")) or next(card_iter)
        )
    else:
        client.cardkit_create = AsyncMock(side_effect=create_error)
    client.reply_card_by_id = AsyncMock(
        side_effect=lambda *a, **k: calls.append(("reply", "")) or next(message_iter)
    )
    client.cardkit_close_streaming = AsyncMock(
        side_effect=lambda card_id, **k: calls.append(("close", card_id))
    )
    client.cardkit_update = AsyncMock(
        side_effect=lambda card_id, *a, **k: calls.append(("seal", card_id))
    )
    return calls


# ── Dispatch 测试 — 流式卡片分流 ──


class TestDispatch:
    """验证流式卡片 session 的入口会消费事件并更新 SegmentState."""

    @pytest.mark.parametrize("event,kwargs,seg_type", [
        ("on_reasoning", {"text": "r"}, "reasoning"),
        ("on_answer", {"text": "a"}, "answer"),
    ])
    def test_dispatch_creates_segment(self, event: str, kwargs: dict, seg_type: str) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_d")
        ctrl._sessions["msg_d"] = session
        assert getattr(ctrl, event)(message_id="msg_d", **kwargs) is True
        assert session.segment_state.segments[0].type == seg_type

    def test_thinking_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_t")
        ctrl._sessions["msg_t"] = session
        with patch.object(ctrl, "_on_thinking_segment") as m:
            m.return_value = True
            ctrl.on_thinking(message_id="msg_t", text="thinking")
            m.assert_called_once()

    def test_tool_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_tool")
        ctrl._sessions["msg_tool"] = session
        assert ctrl.on_tool_update(message_id="msg_tool", tool_name="read", status="started") is True
        assert session.segment_state.segments[0].type == "tool"

    def test_completed_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_c")
        session.state = SessionState.STREAMING
        session.card_id = "card_123"
        ctrl._sessions["msg_c"] = session
        with (
            patch.object(ctrl, "_do_complete_card", new_callable=AsyncMock),
            patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()),
        ):
            ctrl.on_completed(message_id="msg_c")
        assert session.flush._completed

    def test_session_without_segment_state_not_consumed(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_no_state")
        session.segment_state = None
        ctrl._sessions["msg_no_state"] = session
        assert ctrl.on_answer(message_id="msg_no_state", text="answer text") is False
        assert session.segment_state is None

    def test_message_started_consumes_delta_before_create_task_runs(self) -> None:
        ctrl = _setup_ctrl()

        with patch.object(ctrl, "_fire_and_forget", side_effect=lambda coro, loop: coro.close()):
            ctrl.on_message_started(message_id="msg_early_delta", chat_id="chat1")

        session = ctrl._sessions["msg_early_delta"]
        assert session.segment_state is not None
        assert ctrl.on_answer(message_id="msg_early_delta", text="early") is True
        assert session.segment_state.segments[0].text == "early"

    def test_guard_skips_terminal(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_term")
        session.state = SessionState.COMPLETED
        ctrl._sessions["msg_term"] = session
        assert ctrl.on_answer(message_id="msg_term", text="late text") is False
        assert len(session.segment_state.segments) == 0

    def test_message_started_creates_session(self) -> None:
        ctrl = _setup_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        session = ctrl._sessions["msg1"]
        loop = session._loop
        loop.run_until_complete(asyncio.sleep(0.05))
        assert session.segment_state is not None
        assert session.card_id is not None


# ── _do_create_card 集成测试 ──


class TestDoCreateCard:
    @pytest.mark.asyncio
    async def test_cardkit_success(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_create")
        ctrl._sessions["msg_create"] = session

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.card_id == "card_id_abc"
        assert session.state == SessionState.STREAMING

    @pytest.mark.asyncio
    async def test_cardkit_failure_yields_to_gateway(self) -> None:
        ctrl = _setup_ctrl()
        client = ctrl._client
        client.cardkit_create = AsyncMock(side_effect=FeishuAPIError("fail", code=230099))
        session = _make_session("msg_fallback")
        ctrl._sessions["msg_fallback"] = session

        await ctrl._do_create_card(session)

        assert session.segment_state is not None
        assert session.state == SessionState.FAILED
        assert await ctrl.on_completed_wait(message_id="msg_fallback", answer="plain") is False
        assert ctrl.consume_text_fallback("msg_fallback") is True
        assert ctrl.consume_text_fallback("msg_fallback") is False
        assert "msg_fallback" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_generic_failure_marks_failed(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._client = None
        session = _make_session("msg_err")
        ctrl._sessions["msg_err"] = session

        await ctrl._do_create_card(session)

        assert session.state == SessionState.FAILED

    @pytest.mark.asyncio
    async def test_segment_state_set_before_await(self) -> None:
        """CREATING 期间的事件进入流式卡片路径 — segment_state 在 try 之前设置."""
        ctrl = _setup_ctrl()
        session = _make_session("msg_early")
        ctrl._sessions["msg_early"] = session

        original_ensure = ctrl._ensure_init

        async def check_state_then_ensure() -> None:
            assert session.segment_state is not None
            await original_ensure()

        ctrl._ensure_init = check_state_then_ensure  # type: ignore[assignment]
        await ctrl._do_create_card(session)

    @pytest.mark.asyncio
    async def test_post_create_flush_on_dirty(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_dirty")
        ctrl._sessions["msg_dirty"] = session

        original_ensure = ctrl._ensure_init

        async def inject_data_and_ensure() -> None:
            await original_ensure()
            session.segment_state.on_reasoning_delta("during-creating")

        ctrl._ensure_init = inject_data_and_ensure  # type: ignore[assignment]

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
        ctrl._sessions["msg_flush"] = session

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
        ctrl._sessions["msg_no_split"] = session

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
        ctrl._sessions["msg_split"] = session

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
        ctrl._sessions["msg_tool_roll"] = session

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
        ctrl._sessions["msg_tool_many"] = session

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
        ctrl._sessions["msg_tool_roll_fallback"] = session

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
        ctrl._sessions["msg_split_fallback"] = session

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
    async def test_reasoning_finalized_snapshot(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_snap")
        session.state = SessionState.STREAMING
        session.card_id = "card_snap"
        session.segment_state.on_reasoning_delta("think")
        session.segment_state.on_answer_delta("reply")
        session.segment_state.segments[0].elapsed_ms = 1500.0
        session.segment_state.segments[0].reasoning_finalized = False
        ctrl._sessions["msg_snap"] = session

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
        ctrl._sessions["msg_title"] = session

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
        ctrl._sessions["msg_tool_snap"] = session

        await ctrl._do_flush(session)

        assert tool_seg_ref.tool_end_offset == 5
        assert tool_seg_ref.dirty is True

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
        ctrl._sessions["msg_exc"] = session

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
        ctrl._sessions["msg_err"] = session

        await ctrl._do_flush(session)

    @pytest.mark.asyncio
    async def test_skip_conditions(self) -> None:
        """终态 / 无 card_id / 无 dirty 全部跳过 API 调用."""
        ctrl = _setup_ctrl()

        # 终态
        s1 = _make_session("m1")
        s1.state = SessionState.COMPLETED
        ctrl._sessions["m1"] = s1
        await ctrl._do_flush(s1)

        # 无 card_id
        s2 = _make_session("m2")
        s2.state = SessionState.STREAMING
        s2.card_id = None
        ctrl._sessions["m2"] = s2
        await ctrl._do_flush(s2)

        # 无 dirty
        s3 = _make_session("m3")
        s3.state = SessionState.STREAMING
        s3.card_id = "c"
        s3.segment_state.on_reasoning_delta("t")
        s3.segment_state.segments[0].created = True
        s3.segment_state.segments[0].dirty = False
        ctrl._sessions["m3"] = s3
        await ctrl._do_flush(s3)

        ctrl._client.cardkit_batch_update.assert_not_called()
        ctrl._client.cardkit_stream_element.assert_not_called()


# ── _do_complete_card 集成测试 ──


class TestDoCompleteCard:
    @pytest.mark.asyncio
    async def test_closes_streaming_then_updates(self) -> None:
        ctrl = _setup_ctrl()
        call_order: list[str] = []
        client = ctrl._client
        client.cardkit_close_streaming = AsyncMock(side_effect=lambda *a, **k: call_order.append("close"))
        client.cardkit_update = AsyncMock(side_effect=lambda *a, **k: call_order.append("update"))

        session = _make_session("msg_comp")
        session.state = SessionState.STREAMING
        session.card_id = "card_comp"
        session.card_msg_id = "msg_comp_reply"
        ctrl._sessions["msg_comp"] = session

        assert await ctrl._do_complete_card(session) is True
        assert session.state == SessionState.COMPLETED
        assert call_order == ["close", "update"]

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
        ctrl._sessions["msg_retry"] = session

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
        ctrl._sessions["msg_3fail"] = session

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
        time.sleep(0.001)
        ctrl._sessions["msg_fc"] = session

        await ctrl._do_complete_card(session)

        assert session.segment_state.segments[0].elapsed_ms > 0
        assert "msg_fc" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_no_card_id_skips_close(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_nocard")
        session.state = SessionState.STREAMING
        session.card_id = None
        ctrl._sessions["msg_nocard"] = session

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
        ctrl._sessions["msg_img"] = session

        await ctrl._do_complete_card(session)

        assert resolver.resolve_await.call_count == 2


# ── _on_thinking_segment 集成测试 ──


class TestOnThinking:
    def test_splits_and_dispatches(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_think")
        ctrl._sessions["msg_think"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "<thinking>reasoning here</thinking>\nanswer text")

        types = [s.type for s in session.segment_state.segments]
        assert types == ["reasoning", "answer"]

    def test_empty_text_no_flush(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_think2")
        ctrl._sessions["msg_think2"] = session

        with patch.object(ctrl, "_schedule_flush") as m:
            ctrl._on_thinking_segment(session, "")
            m.assert_not_called()

    def test_none_segment_state_skips(self) -> None:
        ctrl = _setup_ctrl()
        session = _make_session("msg_think3")
        session.segment_state = None
        ctrl._sessions["msg_think3"] = session

        ctrl._on_thinking_segment(session, "some text")

    def test_show_reasoning_false_skips_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": False}}}}  # type: ignore[assignment]
        session = _make_session("msg_noreas")
        ctrl._sessions["msg_noreas"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "<thinking>secret thoughts</thinking>\nreal answer")

        assert all(s.type == "answer" for s in session.segment_state.segments)

    def test_reasoning_only_with_show_reasoning(self) -> None:
        ctrl = _setup_ctrl()
        ctrl._cfg._reload = lambda: {"display": {"platforms": {"feishu": {"show_reasoning": True}}}}  # type: ignore[assignment]
        session = _make_session("msg_ronly")
        ctrl._sessions["msg_ronly"] = session

        with patch.object(ctrl, "_schedule_flush"):
            ctrl._on_thinking_segment(session, "Reasoning:\njust thinking")

        assert len(session.segment_state.segments) == 1
        assert session.segment_state.segments[0].type == "reasoning"


class TestCronDeliver:
    def test_returns_false_when_disabled(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = False
        assert ctrl.on_cron_deliver(chat_id="c1", content="text", loop=MagicMock()) is False

    def test_returns_false_on_empty_content(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True
        assert ctrl.on_cron_deliver(chat_id="c1", content="", loop=MagicMock()) is False

    def test_sends_card_on_success(self) -> None:
        import threading

        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            result = ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is True
            mock_client.send_card_to_chat.assert_called_once()
            args = mock_client.send_card_to_chat.call_args[0]
            assert args[0] == "c1"
            card = args[1]
            assert card["schema"] == "2.0"
            assert "hello" in card["body"]["elements"][0]["content"]
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def test_returns_false_on_send_failure(self) -> None:
        import threading

        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.side_effect = RuntimeError("API error")
        ctrl._client = mock_client
        ctrl._initialized = True

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            result = ctrl.on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is False
        finally:
            loop.call_soon_threadsafe(loop.stop)


class TestBackgroundDeliver:
    @pytest.mark.asyncio
    async def test_sends_cleaned_text_card(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        result = await ctrl.on_background_deliver(
            chat_id="c1",
            preview="prompt",
            content="Here\n\nDone",
            reply_to_message_id="om_1",
        )

        assert result is True
        mock_client.upload_image.assert_not_called()
        mock_client.send_card_to_chat.assert_awaited_once()
        args, kwargs = mock_client.send_card_to_chat.call_args
        assert args[0] == "c1"
        assert kwargs["reply_to_message_id"] == "om_1"
        card = args[1]
        body = card["body"]["elements"][0]["content"]
        assert "Here\n\nDone" in body

    @pytest.mark.asyncio
    async def test_returns_false_on_empty_cleaned_text(self) -> None:
        ctrl = StreamCardController()
        ctrl._cfg = MagicMock()
        ctrl._cfg.enabled = True

        mock_client = AsyncMock()
        mock_client.send_card_to_chat.return_value = "msg_123"
        ctrl._client = mock_client
        ctrl._initialized = True

        result = await ctrl.on_background_deliver(
            chat_id="c1",
            preview="prompt",
            content="",
        )

        assert result is False
        mock_client.upload_image.assert_not_called()
        mock_client.send_card_to_chat.assert_not_called()
