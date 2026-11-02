"""回合状态、并发和终态的场景测试."""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from hermes_lark_streaming.config import Config
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError, FeishuClient
from hermes_lark_streaming.streaming.segments import SegmentType
from hermes_lark_streaming.streaming.tooluse import ToolStatus


@pytest.fixture
def controller(tmp_path):
    cfg = Config(tmp_path)
    client = create_autospec(FeishuClient, instance=True)
    client.cardkit_create.return_value = "card"
    client.reply_card_by_id.return_value = "message"
    client.send_card_to_chat.return_value = "message"
    return StreamCardController(cfg, client)


@pytest.mark.asyncio
async def test_cumulative_frames_and_final_are_not_duplicated(controller):
    for text in ["", "你好", "你好世界"]:
        assert await controller.frame("turn", "chat", text, reply_to="in", source_id="in")
    assert await controller.frame("turn", "chat", "你好世界", reply_to="in", source_id="in", finalize=True)
    card = controller._client.cardkit_update.call_args.args[1]
    assert "你好世界" in str(card)
    assert not controller.turns
    calls = controller._client.cardkit_update.call_count
    assert await controller.frame("turn", "chat", "你好世界", reply_to="in", source_id="in", finalize=True)
    assert controller._client.cardkit_update.call_count == calls


@pytest.mark.asyncio
async def test_authoritative_final_replaces_preamble(controller):
    await controller.frame("t", "chat", "我查一下", reply_to="in", source_id="in")
    await controller.frame("t", "chat", "最终答案", reply_to="in", source_id="in", finalize=True)
    card = str(controller._client.cardkit_update.call_args.args[1])
    assert "最终答案" in card and "我查一下" not in card


@pytest.mark.asyncio
async def test_same_chat_turns_are_isolated_and_stop_is_precise(controller):
    await controller.frame("a", "chat", "A", reply_to="quote", source_id="user-a", session_key="session-a")
    await controller.frame("b", "chat", "B", reply_to="quote", source_id="user-b", session_key="session-b")
    await controller.close_source("user-a")
    assert "a" not in controller.turns and "b" in controller.turns
    assert controller.turns["b"].text == "B"
    await controller.close()


@pytest.mark.asyncio
async def test_failed_create_does_not_ack_delivery(controller):
    controller._client.cardkit_create.side_effect = FeishuAPIError("no permission")
    assert not await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    assert not controller.turns


@pytest.mark.asyncio
async def test_anchorless_frame_sends_to_chat(controller):
    assert await controller.frame("t", "chat", "answer", reply_to=None, source_id="synthetic", finalize=True)
    controller._client.reply_card_by_id.assert_not_called()
    controller._client.send_card_to_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_parallel_same_name_tools_use_call_identity(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    for call_id in ("one", "two"):
        await controller.observe("in", "tool_start", {"tool_name": "terminal", "tool_call_id": call_id})
    await controller.observe(
        "in",
        "tool_end",
        {
            "tool_name": "terminal",
            "tool_call_id": "one",
            "duration_ms": 42,
            "status": "error",
        },
    )
    turn = controller.turns["t"]
    assert turn.tools["one"].status == ToolStatus.ERROR
    assert turn.tools["two"].status == ToolStatus.RUNNING
    await controller.close()


@pytest.mark.asyncio
async def test_late_tool_result_updates_original_panel_without_duplicating(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    payload = {"tool_name": "terminal", "tool_call_id": "one"}
    await controller.observe("in", "tool_start", payload)
    await controller.frame("t", "chat", "answer", reply_to="in", source_id="in")
    session = controller.turns["t"].session
    await session.flush.flush_now(lambda: controller._do_flush(session))
    assert not session.segment_state.has_dirty
    await controller.observe("in", "tool_end", payload)
    await controller.observe("in", "tool_start", payload)  # 重放已结束工具的开始事件
    assert len(session.tool_use.build_display_steps()) == 1
    assert controller.turns["t"].tools["one"].status == ToolStatus.SUCCESS
    tool_segments = [s for s in session.segment_state.segments if s.type == SegmentType.TOOL]
    assert len(tool_segments) == 1 and tool_segments[0].dirty
    await controller.frame("t", "chat", "answer", reply_to="in", source_id="in", finalize=True)
    elements = controller._client.cardkit_update.call_args.args[1]["body"]["elements"]
    assert len([e for e in elements if e["tag"] == "collapsible_panel"]) == 1


@pytest.mark.asyncio
async def test_usage_dedup_and_child_turn_does_not_pollute(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    await controller.observe("in", "start", {"turn_id": "root"})
    payload = {"turn_id": "root", "api_request_id": "r", "usage": {"input_tokens": 10, "output_tokens": 2}}
    await controller.observe("in", "usage", payload)
    await controller.observe("in", "usage", payload)
    await controller.observe("in", "usage", {**payload, "turn_id": "child", "api_request_id": "other"})
    assert controller.turns["t"].session.footer["input_tokens"] == 10
    await controller.close()


@pytest.mark.asyncio
async def test_stop_and_late_frame_do_not_reopen_card(controller):
    await controller.frame("t", "chat", "partial", reply_to="in", source_id="in")
    await controller.observe("in", "end", {"interrupted": True, "completed": False})
    assert await controller.frame("t", "chat", "late", reply_to="in", source_id="in")
    assert await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    assert controller._client.cardkit_create.call_count == 1
    assert not controller.turns


@pytest.mark.asyncio
async def test_explicit_seed_reopens_after_approval_boundary(controller):
    await controller.frame("t", "chat", "等待确认", reply_to="in", source_id="in", finalize=True)
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    await controller.frame("t", "chat", "继续回答", reply_to="in", source_id="in", finalize=True)
    assert controller._client.cardkit_create.call_count == 2


@pytest.mark.asyncio
async def test_reasoning_visibility_and_order(controller, tmp_path):
    (tmp_path / "config.yaml").write_text("display:\n  show_reasoning: true\n")
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    await controller.observe("in", "reasoning", {"delta": "思考"})
    await controller.frame("t", "chat", "正文", reply_to="in", source_id="in")
    segments = controller.turns["t"].session.segment_state.segments
    assert [s.type for s in segments] == [SegmentType.REASONING, SegmentType.ANSWER]
    await controller.close()


@pytest.mark.asyncio
async def test_concurrent_finalizers_only_send_one_final(controller):
    await controller.frame("t", "chat", "a", reply_to="in", source_id="in")
    await asyncio.gather(
        *[controller.frame("t", "chat", "answer", reply_to="in", source_id="in", finalize=True) for _ in range(3)]
    )
    assert controller._client.cardkit_update.call_count == 1


@pytest.mark.asyncio
async def test_tool_budget_does_not_reassign_the_last_step(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    controller.turns["t"].session.tool_use._max_steps = 1
    for call_id in ("one", "two"):
        await controller.observe("in", "tool_start", {"tool_name": "terminal", "tool_call_id": call_id})
    await controller.observe("in", "tool_end", {"tool_name": "terminal", "tool_call_id": "two", "status": "error"})
    assert controller.turns["t"].tools["one"].status == ToolStatus.RUNNING
    assert "two" not in controller.turns["t"].tools
    await controller.close()


@pytest.mark.asyncio
async def test_hidden_interim_reasoning_does_not_enter_answer(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    assert await controller.interim("in", "<thinking>private</thinking>visible")
    await controller.frame("t", "chat", "final", reply_to="in", source_id="in", finalize=True)
    body = str(controller._client.cardkit_update.call_args.args[1])
    assert "private" not in body and "visible" in body and "final" in body


@pytest.mark.asyncio
async def test_failed_interim_flush_does_not_ack_delivery(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    controller._client.cardkit_batch_update.side_effect = FeishuAPIError("unavailable", code=1664)
    assert await controller.interim("in", "checking") is None
    controller._client.cardkit_batch_update.side_effect = None
    await controller.close()


@pytest.mark.asyncio
async def test_deleted_card_stops_future_flushes(controller):
    await controller.frame("t", "chat", "partial", reply_to="in", source_id="in")
    session = controller.turns["t"].session
    controller._client.cardkit_batch_update.side_effect = FeishuAPIError("deleted", code=231003)
    await session.flush.flush_now(lambda: controller._do_flush(session))
    assert session.state.is_terminal
    calls = controller._client.cardkit_batch_update.call_count
    assert await controller.frame("t", "chat", "late", reply_to="in", source_id="in")
    assert controller._client.cardkit_batch_update.call_count == calls
    assert not controller.turns


@pytest.mark.asyncio
async def test_commentary_already_streamed_is_not_appended_again(controller):
    text = "我会先检查相关文件"
    await controller.frame("t", "chat", text, reply_to="in", source_id="in")
    assert await controller.interim("in", text)
    await controller.frame("t", "chat", text, reply_to="in", source_id="in", finalize=True)
    card = controller._client.cardkit_update.call_args.args[1]
    answers = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
    assert answers.count(text) == 1


@pytest.mark.asyncio
async def test_successful_tool_result_is_hidden_but_error_is_short(controller):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    await controller.observe("in", "tool_start", {"tool_name": "terminal", "tool_call_id": "ok"})
    await controller.observe(
        "in", "tool_end", {"tool_name": "terminal", "tool_call_id": "ok", "result": "long result" * 1000},
    )
    await controller.observe("in", "tool_start", {"tool_name": "terminal", "tool_call_id": "fail"})
    await controller.observe(
        "in", "tool_end",
        {"tool_name": "terminal", "tool_call_id": "fail", "is_error": True, "error_message": "error line\n" * 100},
    )
    steps = controller.turns["t"].session.tool_use.build_display_steps()
    assert steps[0]["status"] == "success" and steps[0]["result_block"] is None
    assert steps[0]["output"] == ""
    assert steps[1]["status"] == "error"
    assert len(steps[1]["error"]) <= 201 and "\n" not in steps[1]["error"]
    await controller.close()


@pytest.mark.asyncio
async def test_rewritten_snapshot_reuses_remote_answer_element(controller):
    await controller.frame("t", "chat", "old content", reply_to="in", source_id="in")
    session = controller.turns["t"].session
    await session.flush.flush_now(lambda: controller._do_flush(session))
    old_id = session.segment_state.segments[0].el_id
    await controller.frame("t", "chat", "corrected content", reply_to="in", source_id="in")
    await session.flush.flush_now(lambda: controller._do_flush(session))
    assert len(session.segment_state.segments) == 1
    assert session.segment_state.segments[0].el_id == old_id
    assert controller._client.cardkit_batch_update.await_count == 1
    assert controller._client.cardkit_stream_element.call_args.args[2] == "corrected content"
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["answer", "reasoning", "image"])
async def test_updates_during_stream_request_remain_dirty(controller, kind):
    await controller.frame("t", "chat", "", reply_to="in", source_id="in")
    session = controller.turns["t"].session
    state = session.segment_state
    if kind == "reasoning":
        state.on_reasoning_delta("first")
    else:
        state.on_answer_delta("first" if kind == "answer" else "![image](https://example.com/image.png)")

    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_request(*args, **kwargs):
        entered.set()
        await release.wait()

    controller._client.cardkit_stream_element.side_effect = hold_request
    if kind == "image":
        controller._client.upload_image.side_effect = hold_request
    flush = asyncio.create_task(session.flush.flush_now(lambda: controller._do_flush(session)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if kind == "image":
            session.image_resolver._cache["https://example.com/image.png"] = "img_uploaded"
            session.image_resolver._on_image_resolved()
        elif kind == "reasoning":
            state.on_reasoning_delta(" second")
        else:
            await controller.frame("t", "chat", "first second", reply_to="in", source_id="in")
        release.set()
        await asyncio.wait_for(flush, 2)
        assert state.has_dirty
        await session.flush.flush_now(lambda: controller._do_flush(session))
        assert not state.has_dirty
        content = controller._client.cardkit_stream_element.call_args.args[2]
        assert ("img_uploaded" if kind == "image" else "first second") in content
    finally:
        release.set()
        await flush
        await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [
    ("cardkit_stream_element", ("card", "answer", "text")),
    ("cardkit_batch_update", ("card", [{"type": "add_elements"}])),
    ("cardkit_update", ("card", {"schema": "2.0"})),
    ("cardkit_close_streaming", ("card",)),
])
async def test_sequence_recovery_preserves_operation_and_next_sequence(controller, method, args):
    from hermes_lark_streaming.streaming.session import CardSession

    session = CardSession("in", "chat", asyncio.get_running_loop())
    operation = getattr(controller._client, method)
    operation.side_effect = [FeishuAPIError("sequence conflict", code=300317), None, None]
    await controller._cardkit_call(session, operation, *args)
    await controller._cardkit_call(session, operation, *args)
    calls = operation.await_args_list
    assert [c.kwargs["sequence"] for c in calls] == [2, 3, 4]
    assert session.sequence == 4
    assert all(c.args == args for c in calls)
    assert calls[0].kwargs["request_id"] == calls[1].kwargs["request_id"]
    assert calls[2].kwargs["request_id"] != calls[1].kwargs["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,attempts", [(300317, 3), (230099, 1), (231003, 1)])
async def test_sequence_recovery_is_bounded_and_only_handles_conflicts(controller, code, attempts):
    from hermes_lark_streaming.streaming.session import CardSession

    session = CardSession("in", "chat", asyncio.get_running_loop())
    operation = controller._client.cardkit_update
    operation.side_effect = FeishuAPIError("failed", code=code)
    with pytest.raises(FeishuAPIError) as error:
        await controller._cardkit_call(session, operation, "card", {})
    assert error.value.code == code
    assert operation.await_count == attempts
    assert len({c.kwargs["request_id"] for c in operation.await_args_list}) == 1
