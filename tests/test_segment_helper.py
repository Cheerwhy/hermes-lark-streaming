"""streaming.segment_helper 测试 — CardKit 元素容量估算与 tool 拆分点."""

from __future__ import annotations

from hermes_lark_streaming.streaming.segment_helper import (
    ELEMENT_THRESHOLD,
    FOOTER_RESERVE,
    estimate_segment_elements,
    estimate_tool_elements,
    find_tool_split_offset,
    tool_segment_end,
)
from hermes_lark_streaming.streaming.segments import Segment, SegmentType
from hermes_lark_streaming.streaming.tooluse import ToolDisplayStep


def _step(*, detail: str = "", result: bool = False, error: bool = False) -> ToolDisplayStep:
    return {
        "name": "read",
        "title": "Read",
        "status": "running",
        "detail": detail,
        "output": "",
        "error": "",
        "icon": "tool",
        "elapsed_ms": 0,
        "result_block": {"language": "text", "content": "ok", "fenced": "ok"} if result else None,
        "error_block": {"language": "text", "content": "boom", "fenced": "boom"} if error else None,
    }


def test_estimate_segment_elements_for_basic_types() -> None:
    reasoning = Segment(SegmentType.REASONING, "reasoning")
    answer = Segment(SegmentType.ANSWER, "answer")
    tool = Segment(SegmentType.TOOL, "tool")
    steps = [_step()]

    assert estimate_segment_elements(reasoning, steps) == 4
    assert estimate_segment_elements(answer, steps) == 1
    assert estimate_segment_elements(tool, steps) == 6


def test_estimate_tool_elements_counts_optional_detail_and_output_blocks() -> None:
    steps = [
        _step(),
        _step(detail="path"),
        _step(result=True),
        _step(error=True),
    ]

    assert estimate_tool_elements(0, len(steps), steps) == 21


def test_tool_segment_end_uses_open_segment_step_count() -> None:
    seg = Segment(SegmentType.TOOL, "tool")
    steps = [_step(), _step()]

    assert tool_segment_end(seg, steps) == 2

    seg.tool_end_offset = 1
    assert tool_segment_end(seg, steps) == 1


def test_find_tool_split_offset_keeps_largest_fitting_prefix() -> None:
    seg = Segment(SegmentType.TOOL, "tool")
    steps = [_step() for _ in range(4)]

    split_offset = find_tool_split_offset(
        base_count=ELEMENT_THRESHOLD - FOOTER_RESERVE - 7,
        seg=seg,
        all_steps=steps,
    )

    assert split_offset == 1


def test_find_tool_split_offset_returns_none_for_single_step() -> None:
    seg = Segment(SegmentType.TOOL, "tool")

    assert find_tool_split_offset(base_count=1, seg=seg, all_steps=[_step()]) is None
