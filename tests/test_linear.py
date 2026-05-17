"""线性模式测试 — 状态机转换、卡片生命周期、多轮流程."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_lark_streaming.config import Config
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.controller_mixin import (
    ABORTED,
    L_ANSWER,
    L_IDLE,
    L_TOOL,
    STREAMING,
)


def _make_config(raw: dict[str, Any]) -> Config:
    cfg = Config()
    cfg._raw = raw
    cfg._reload = lambda: raw
    return cfg


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.cardkit_create = AsyncMock(return_value="card_mock")
    client.reply_card_by_id = AsyncMock(return_value="msg_mock")
    client.reply_card = AsyncMock(return_value="msg_mock")
    client.send_card_by_id = AsyncMock(return_value="tool_msg_mock")
    client.cardkit_stream_element = AsyncMock()
    client.cardkit_close_streaming = AsyncMock()
    client.cardkit_update = AsyncMock()
    client.cardkit_update_element = AsyncMock()
    client.update_card = AsyncMock()
    return client


def _make_linear_ctrl(*, show_reasoning: bool = False) -> StreamCardController:
    raw: dict[str, Any] = {
        "streaming": {"enabled": True, "linear": True},
        "feishu": {"app_id": "app", "app_secret": "secret"},
    }
    if show_reasoning:
        raw["display"] = {"platforms": {"feishu": {"show_reasoning": True}}}
    ctrl = StreamCardController()
    ctrl._cfg._raw = raw
    ctrl._cfg._reload = lambda: raw
    ctrl._initialized = True
    ctrl._client = _make_mock_client()
    return ctrl


async def _drain() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# ── 线性模式初始化 ──


class TestLinearInit:
    @pytest.mark.asyncio
    async def test_session_created_with_linear(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        s = ctrl._sessions["msg1"]
        assert s.linear is True
        assert s._linear_phase == L_IDLE
        assert s.state == STREAMING

    @pytest.mark.asyncio
    async def test_no_card_created_on_start(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        await _drain()
        assert ctrl._client.cardkit_create.call_count == 0

    @pytest.mark.asyncio
    async def test_fields_initialized(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        s = ctrl._sessions["msg1"]
        assert s._segment_start == 0
        assert s._tool_start_idx == 0
        assert s._tool_card_id is None
        assert s._tool_card_msg_id is None


# ── 状态机转换 ──


class TestLinearPhaseTransition:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "trigger_fn",
        [
            # on_answer → L_ANSWER
            lambda ctrl: ctrl.on_answer(message_id="msg1", text="Hello"),
            # on_thinking → L_ANSWER (answer_text 非空)
            lambda ctrl: ctrl.on_thinking(message_id="msg1", text="partial answer"),
            # on_reasoning → L_ANSWER (show_reasoning=True)
            lambda ctrl: ctrl.on_reasoning(message_id="msg1", text="thinking..."),
        ],
    )
    async def test_idle_to_answer(self, trigger_fn: Any) -> None:
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        trigger_fn(ctrl)
        assert ctrl._sessions["msg1"]._linear_phase == L_ANSWER

    @pytest.mark.asyncio
    async def test_idle_to_tool_directly(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        assert ctrl._sessions["msg1"]._linear_phase == L_TOOL

    @pytest.mark.asyncio
    async def test_answer_to_tool(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Checking")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        assert ctrl._sessions["msg1"]._linear_phase == L_TOOL

    @pytest.mark.asyncio
    async def test_tool_to_answer(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Part1")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        ctrl.on_answer(message_id="msg1", text=" Part2")
        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s._segment_start == 5  # len("Part1")

    @pytest.mark.asyncio
    async def test_consecutive_tools_same_phase(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="a.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="started", detail="cmd")
        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_TOOL
        assert len(s.tool_use._session.steps) == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_answer_continuation_appends_text(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Hello")
        ctrl.on_answer(message_id="msg1", text=" World")
        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s.text.display_text == "Hello World"

    @pytest.mark.asyncio
    async def test_on_reasoning_in_answer_does_not_change_phase(self) -> None:
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Hello")
        ctrl.on_reasoning(message_id="msg1", text="more thought")
        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER

    @pytest.mark.asyncio
    async def test_on_answer_ignored_in_terminal_state(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl._sessions["msg1"].state = "completed"
        ctrl.on_answer(message_id="msg1", text="too late")
        s = ctrl._sessions["msg1"]
        assert s.text.display_text == ""


# ── 多轮完整流程 ──


class TestLinearMultiRound:
    @pytest.mark.asyncio
    async def test_answer_tool_answer(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_answer(message_id="msg1", text="Let me read")
        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s._segment_start == 0

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        assert s._linear_phase == L_TOOL

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        assert s._linear_phase == L_TOOL

        ctrl.on_answer(message_id="msg1", text=" Here is the result")
        assert s._linear_phase == L_ANSWER
        assert s._segment_start == 11  # len("Let me read")

    @pytest.mark.asyncio
    async def test_two_tool_rounds(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_answer(message_id="msg1", text="Start")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="a.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        ctrl.on_answer(message_id="msg1", text=" Mid")
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="started", detail="cmd")
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="completed", detail="done")
        ctrl.on_answer(message_id="msg1", text=" End")

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s.text.display_text == "Start Mid End"
        assert len(s.tool_use._session.steps) == 2  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_tool_answer_tool_without_initial_answer(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        assert ctrl._sessions["msg1"]._linear_phase == L_TOOL

        ctrl.on_answer(message_id="msg1", text="Thinking")
        assert ctrl._sessions["msg1"]._linear_phase == L_ANSWER
        assert ctrl._sessions["msg1"]._segment_start == 0

        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="started", detail="cmd")
        assert ctrl._sessions["msg1"]._linear_phase == L_TOOL


# ── 异步卡片操作 ──


class TestLinearCardCreation:
    @pytest.mark.asyncio
    async def test_answer_card_created_via_reply(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Hello")
        await _drain()

        client = ctrl._client
        client.cardkit_create.assert_called()
        client.reply_card_by_id.assert_called_once()
        s = ctrl._sessions["msg1"]
        assert s.card_id == "card_mock"
        assert s.card_msg_id == "msg_mock"

    @pytest.mark.asyncio
    async def test_tool_card_created_via_send(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        client = ctrl._client
        client.send_card_by_id.assert_called_once()
        s = ctrl._sessions["msg1"]
        assert s._tool_card_id == "card_mock"
        assert s._tool_card_msg_id == "tool_msg_mock"

    @pytest.mark.asyncio
    async def test_answer_to_tool_transitions_cards(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Checking")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_TOOL
        assert s.card_id is None
        assert s.card_msg_id is None
        assert s._tool_card_id is not None

    @pytest.mark.asyncio
    async def test_tool_to_answer_transitions_cards(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Start")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        ctrl.on_answer(message_id="msg1", text=" More")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s._tool_card_id is None
        assert s.card_id is not None

    @pytest.mark.asyncio
    async def test_tool_start_idx_advanced_after_close(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="a.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        await _drain()

        ctrl.on_answer(message_id="msg1", text="Result")
        await _drain()

        assert ctrl._sessions["msg1"]._tool_start_idx == 1

    @pytest.mark.asyncio
    async def test_second_tool_round_offset(self) -> None:
        """第二轮工具调用只显示新步骤."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        # 第一轮
        ctrl.on_answer(message_id="msg1", text="A1")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="a.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        await _drain()

        # 回答 → 关闭工具卡
        ctrl.on_answer(message_id="msg1", text=" A2")
        await _drain()

        # 第二轮工具
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="started", detail="cmd")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s._tool_start_idx == 1  # read 已关闭
        steps = s.tool_use.build_display_steps(s._tool_start_idx)
        assert len(steps) == 1
        assert steps[0]["name"] == "exec"

    @pytest.mark.asyncio
    async def test_segment_text_on_close(self) -> None:
        """关闭回答卡时只发送当前段文本."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_answer(message_id="msg1", text="Round1")
        await _drain()

        # 进入工具 → 关闭回答卡
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        client = ctrl._client
        close_calls = client.cardkit_close_streaming.call_args_list
        assert len(close_calls) >= 1
        update_calls = client.cardkit_update.call_args_list
        assert any("Round1" in str(c) for c in update_calls)


# ── 完成 ──


class TestLinearComplete:
    @pytest.mark.asyncio
    async def test_complete_in_answer_phase(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Final answer")
        await _drain()

        result = ctrl.on_completed(
            message_id="msg1", answer="Final answer", duration=1.0, model="test",
        )
        await _drain()

        assert result is True
        assert "msg1" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_complete_in_tool_phase(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Partial")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        await _drain()

        result = ctrl.on_completed(
            message_id="msg1", answer="Partial", duration=2.0, model="test",
        )
        await _drain()

        assert result is True
        assert "msg1" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_complete_no_cards_no_text(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        result = ctrl.on_completed(
            message_id="msg1", answer="", duration=0.5, model="test",
        )
        await _drain()

        assert result is True

    @pytest.mark.asyncio
    async def test_complete_finalizes_with_footer(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Done")
        await _drain()

        ctrl.on_completed(
            message_id="msg1",
            answer="Done",
            duration=1.5,
            model="claude-sonnet-4-20250514",
            tokens={"input_tokens": 100, "output_tokens": 50},
        )
        await _drain()

        client = ctrl._client
        update_calls = client.cardkit_update.call_args_list
        all_args = str(update_calls)
        assert "claude-sonnet" in all_args or "Done" in all_args

    @pytest.mark.asyncio
    async def test_complete_after_multi_round(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_answer(message_id="msg1", text="Start")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="completed", detail="ok")
        ctrl.on_answer(message_id="msg1", text=" Middle")
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="started", detail="cmd")
        ctrl.on_tool_update(message_id="msg1", tool_name="exec", status="completed", detail="done")
        ctrl.on_answer(message_id="msg1", text=" Final")
        await _drain()

        result = ctrl.on_completed(
            message_id="msg1", answer="Start Middle Final", duration=3.0, model="test",
        )
        await _drain()

        assert result is True


# ── 推理相关 ──


class TestLinearReasoning:
    @pytest.mark.asyncio
    async def test_thinking_mixed_reasoning_and_answer(self) -> None:
        """on_thinking 含 <thinking>reasoning</thinking>answer 触发 L_ANSWER."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_thinking(message_id="msg1", text="<thinking>hmm</thinking>answer text")

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s.reasoning_text == "hmm"
        assert "answer text" in s.text.display_text

    @pytest.mark.asyncio
    async def test_thinking_tags_still_triggers_answer(self) -> None:
        """<thinking>hmm</thinking> 被 strip 后 answer_text 非空，仍然触发 L_ANSWER."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_thinking(message_id="msg1", text="<thinking>hmm</thinking>")

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert s.reasoning_text == "hmm"

    @pytest.mark.asyncio
    async def test_thinking_reasoning_prefix_no_phase_change(self) -> None:
        """Reasoning:\\n 前缀的整段文本都视为 reasoning，不触发 L_ANSWER."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_thinking(message_id="msg1", text="Reasoning:\nstep 1\n\nactual answer")

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_IDLE
        assert "step 1" in s.reasoning_text

    @pytest.mark.asyncio
    async def test_reasoning_flushed_into_answer_card(self) -> None:
        """推理文本在回答卡创建时 flush 到 reasoning 面板."""
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_reasoning(message_id="msg1", text="thinking step 1")
        ctrl.on_reasoning(message_id="msg1", text=" thinking step 2")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s._linear_phase == L_ANSWER
        assert "step 1" in s.reasoning_text
        assert s.reasoning_panel_added is True

        client = ctrl._client
        stream_calls = client.cardkit_stream_element.call_args_list
        reasoning_calls = [c for c in stream_calls if c[0][1] == "reasoning_text"]
        assert len(reasoning_calls) >= 1

    @pytest.mark.asyncio
    async def test_reasoning_included_in_closed_card(self) -> None:
        """关闭回答卡时 reasoning 包含在完成态卡片中."""
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_reasoning(message_id="msg1", text="deep thought")
        ctrl.on_answer(message_id="msg1", text="Result")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        client = ctrl._client
        update_calls = client.cardkit_update.call_args_list
        all_content = str(update_calls)
        assert "deep thought" in all_content

    @pytest.mark.asyncio
    async def test_reasoning_reset_between_rounds(self) -> None:
        """多轮间 reasoning_text 在关闭回答卡后重置."""
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_reasoning(message_id="msg1", text="thought round 1")
        ctrl.on_answer(message_id="msg1", text="Answer1")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s.reasoning_text == ""
        assert s.reasoning_start == 0.0
        assert s.reasoning_panel_added is False

        ctrl.on_reasoning(message_id="msg1", text="thought round 2")
        ctrl.on_answer(message_id="msg1", text=" Answer2")
        await _drain()

        assert s.reasoning_text == "thought round 2"
        assert s.reasoning_panel_added is True

    @pytest.mark.asyncio
    async def test_reasoning_in_answer_schedules_update(self) -> None:
        """L_ANSWER 且 card 已创建时，on_reasoning 调度 reasoning update."""
        ctrl = _make_linear_ctrl(show_reasoning=True)
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_reasoning(message_id="msg1", text="initial")
        await _drain()

        s = ctrl._sessions["msg1"]
        assert s.card_id is not None
        assert s._linear_phase == L_ANSWER

        ctrl.on_reasoning(message_id="msg1", text=" more thought")
        assert s._linear_phase == L_ANSWER
        assert s.reasoning_dirty is True
        assert "more thought" in s.reasoning_text


# ── /stop 中止 ──


class TestLinearAbort:
    @pytest.mark.asyncio
    async def test_abort_sets_aborted_state(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Working")
        await _drain()

        ctrl.on_aborted(message_id="msg1")
        s = ctrl._sessions["msg1"]
        assert s.state == ABORTED

    @pytest.mark.asyncio
    async def test_abort_in_answer_phase_closes_card(self) -> None:
        """/stop 在回答阶段：关闭回答卡并 cleanup."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Partial answer")
        await _drain()

        assert ctrl._sessions["msg1"].card_id is not None

        ctrl.on_aborted(message_id="msg1")
        await _drain()

        assert "msg1" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_abort_in_tool_phase_closes_tool_card(self) -> None:
        """/stop 在工具阶段：先关工具卡再 cleanup."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Checking")
        await _drain()

        ctrl.on_tool_update(message_id="msg1", tool_name="read", status="started", detail="f.py")
        await _drain()

        assert ctrl._sessions["msg1"]._tool_card_id is not None

        ctrl.on_aborted(message_id="msg1")
        await _drain()

        assert "msg1" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_abort_no_card_still_cleans_up(self) -> None:
        """/stop 在 L_IDLE（未创建卡片）：仍正常 cleanup."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")

        ctrl.on_aborted(message_id="msg1")
        await _drain()

        assert "msg1" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_abort_triggers_cardkit_close(self) -> None:
        """/stop 后 cardkit_close_streaming 被调用."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="msg1", chat_id="chat1")
        ctrl.on_answer(message_id="msg1", text="Text")
        await _drain()

        ctrl.on_aborted(message_id="msg1")
        await _drain()

        ctrl._client.cardkit_close_streaming.assert_called()


# ── 新消息打断 ──


class TestLinearInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_creates_linear_session(self) -> None:
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="old", chat_id="chat1")
        ctrl.on_answer(message_id="old", text="Partial")

        ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="chat1")

        new_s = ctrl._sessions.get("new")
        assert new_s is not None
        assert new_s.linear is True
        assert new_s.state == STREAMING

    @pytest.mark.asyncio
    async def test_interrupt_aborts_old_session(self) -> None:
        """旧 session 状态设为 ABORTED 并触发 complete."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="old", chat_id="chat1")
        ctrl.on_answer(message_id="old", text="Partial")
        await _drain()

        ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="chat1")
        await _drain()

        assert "old" not in ctrl._sessions
        assert "new" in ctrl._sessions

    @pytest.mark.asyncio
    async def test_interrupt_in_tool_phase(self) -> None:
        """打断发生在工具阶段：工具卡被关闭."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="old", chat_id="chat1")
        ctrl.on_answer(message_id="old", text="Checking")
        await _drain()

        ctrl.on_tool_update(message_id="old", tool_name="read", status="started", detail="f.py")
        await _drain()

        assert ctrl._sessions["old"]._tool_card_id is not None

        ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="chat1")
        await _drain()

        assert "old" not in ctrl._sessions

    @pytest.mark.asyncio
    async def test_interrupt_sets_interrupt_map(self) -> None:
        """打断映射 old→new 被正确设置."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="old", chat_id="chat1")

        ctrl.on_interrupted(old_message_id="old", new_message_id="new", chat_id="chat1")

        assert ctrl._interrupt_map.get("old") == "new"

    @pytest.mark.asyncio
    async def test_interrupt_chain_redirects(self) -> None:
        """连续打断 A→B→C：interrupt_map 链式更新."""
        ctrl = _make_linear_ctrl()
        ctrl.on_message_started(message_id="A", chat_id="chat1")

        ctrl.on_interrupted(old_message_id="A", new_message_id="B", chat_id="chat1")
        assert ctrl._interrupt_map.get("A") == "B"

        ctrl.on_interrupted(old_message_id="B", new_message_id="C", chat_id="chat1")
        assert ctrl._interrupt_map.get("A") == "C"
        assert ctrl._interrupt_map.get("B") == "C"
