"""linear.py 测试 — LinearState 段管理、边界条件、多轮集成."""

from __future__ import annotations

import time

import pytest

from hermes_lark_streaming import linear as linear_module
from hermes_lark_streaming.linear import LinearState, Segment


class TestSegmentDefaults:
    def test_all_defaults(self) -> None:
        for seg_type, el_id in [("reasoning", "r_0_panel"), ("answer", "a_0"), ("tool", "t_0")]:
            seg = Segment(seg_type, el_id)
            assert seg.type == seg_type
            assert seg.el_id == el_id
            assert seg.created is False
            assert seg.dirty is True
            assert seg.element_estimate == 0
            assert seg.text == ""
            assert seg.tool_offset == 0
            assert seg.tool_end_offset == 0
            assert seg.elapsed_ms == 0.0
            assert seg.reasoning_finalized is False

    def test_slots_no_dynamic_attr(self) -> None:
        seg = Segment("answer", "a_0")
        with pytest.raises(AttributeError):
            seg.nonexistent = True  # type: ignore[attr-defined]


# ── 段创建与追加（三种类型共享同一模式：空→新建、同类型→追加、异类型→新建） ──


class TestOnReasoningDelta:
    def test_appends_or_creates(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("hello ")
        state.on_reasoning_delta("world")
        assert len(state.segments) == 1
        assert state.segments[0].text == "hello world"

    def test_new_segment_when_last_is_answer(self) -> None:
        state = LinearState()
        state.on_answer_delta("answer")
        state.on_reasoning_delta("thoughts")
        assert len(state.segments) == 2
        assert state.segments[0].type == "answer"
        assert state.segments[1].type == "reasoning"


class TestOnAnswerDelta:
    def test_appends_or_creates(self) -> None:
        state = LinearState()
        state.on_answer_delta("hello ")
        state.on_answer_delta("world")
        assert len(state.segments) == 1
        assert state.segments[0].text == "hello world"

    def test_new_segment_when_last_is_reasoning(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("thinking")
        state.on_answer_delta("reply")
        assert len(state.segments) == 2
        assert state.segments[0].type == "reasoning"
        assert state.segments[1].type == "answer"

    def test_finalizes_prev_reasoning_elapsed(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("thinking")
        time.sleep(0.01)
        state.on_answer_delta("reply")
        assert state.segments[0].elapsed_ms > 0

    def test_does_not_finalize_already_finalized(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("a")
        state.on_answer_delta("b")
        elapsed_1 = state.segments[0].elapsed_ms
        state.on_reasoning_delta("c")
        state.on_answer_delta("d")
        assert state.segments[0].elapsed_ms == elapsed_1


class TestOnToolEvent:
    @pytest.mark.parametrize("count", [0, -1])
    def test_non_positive_returns_early(self, count: int) -> None:
        state = LinearState()
        state.on_tool_event(count)
        assert state.segments == []

    def test_creates_and_marks_dirty(self) -> None:
        state = LinearState()
        state.on_tool_event(3)
        assert len(state.segments) == 1
        assert state.segments[0].type == "tool"
        assert state.segments[0].tool_offset == 2
        assert state.segments[0].dirty is True

    def test_same_type_marks_dirty(self) -> None:
        state = LinearState()
        state.on_tool_event(1)
        state.segments[0].dirty = False
        state.on_tool_event(2)
        assert len(state.segments) == 1
        assert state.segments[0].dirty is True

    def test_cross_type_creates_new_and_finalizes_prev(self) -> None:
        state = LinearState()
        state.on_tool_event(2)  # tool1: offset=1
        state.on_answer_delta("intermediate")
        state.on_tool_event(5)  # tool2: offset=4, tool1.end=4
        assert state.segments[0].tool_end_offset == 4
        assert state.segments[2].tool_offset == 4


# ── finalize_segments ──


class TestFinalizeSegments:
    def test_finalizes_last_tool_and_reasoning(self) -> None:
        state = LinearState()
        state.on_tool_event(1)
        state.on_reasoning_delta("thinking")
        time.sleep(0.001)
        state.finalize_segments(3)
        assert state.segments[0].tool_end_offset == 3
        assert state.segments[1].elapsed_ms > 0

    def test_no_segments_no_error(self) -> None:
        state = LinearState()
        state.finalize_segments(0)

    def test_already_finalized_not_overwritten(self) -> None:
        state = LinearState()
        state.on_tool_event(2)
        state.on_answer_delta("mid")
        state.on_tool_event(5)
        state.on_reasoning_delta("a")
        time.sleep(0.01)
        state.on_answer_delta("b")
        # segments: [tool(0), answer(1), tool(2), reasoning(3), answer(4)]
        elapsed_r1 = state.segments[3].elapsed_ms
        state.finalize_segments(10)
        assert state.segments[0].tool_end_offset == 4  # finalized by tool[2], not overwritten
        assert state.segments[2].tool_end_offset == 10  # finalized by finalize
        assert state.segments[3].elapsed_ms == elapsed_r1  # reasoning not overwritten


# ── has_dirty ──


class TestHasDirty:
    def test_dirty_lifecycle(self) -> None:
        state = LinearState()
        assert state.has_dirty is False
        state.on_reasoning_delta("a")
        assert state.has_dirty is True
        state.segments[0].dirty = False
        assert state.has_dirty is False


# ── 多轮集成 ──


class TestMultiRound:
    def test_two_rounds(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("think 1")
        state.on_answer_delta("reply 1")
        state.on_tool_event(2)
        state.on_reasoning_delta("think 2")
        state.on_answer_delta("reply 2")
        types = [s.type for s in state.segments]
        assert types == ["reasoning", "answer", "tool", "reasoning", "answer"]

    def test_el_id_naming_persists(self) -> None:
        state = LinearState()
        state.on_reasoning_delta("a")  # 0
        state.on_answer_delta("b")  # 1
        state.on_tool_event(1)  # 2
        state.on_reasoning_delta("c")  # 3
        assert state.segments[0].el_id == "reasoning_0_panel"
        assert state.segments[0].text_el_id == "reasoning_0_text"
        assert state.segments[1].el_id == "answer_1"
        assert state.segments[2].el_id == "tools_2"
        assert state.segments[3].el_id == "reasoning_3_panel"

    def test_finalize_complex_scenario(self, monkeypatch: pytest.MonkeyPatch) -> None:
        times = iter(float(i) for i in range(100, 108))
        monkeypatch.setattr(linear_module.time, "time", lambda: next(times))

        state = LinearState()
        state.on_reasoning_delta("r1")
        state.on_answer_delta("a1")
        state.on_tool_event(2)  # tool1: offset=1
        state.on_answer_delta("mid")
        state.on_tool_event(4)  # tool2: offset=3, tool1.end=3
        state.on_reasoning_delta("r2")
        state.on_answer_delta("a2")
        state.finalize_segments(5)

        assert state.segments[0].elapsed_ms > 0  # r1 finalized by a1
        assert state.segments[2].tool_end_offset == 3  # tool1 finalized by tool2
        assert state.segments[4].tool_end_offset == 5  # tool2 finalized by finalize
        assert state.segments[5].elapsed_ms > 0  # r2 finalized by a2
