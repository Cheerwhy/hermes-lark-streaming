"""Segment 对应的 CardKit action 构造与容量估算."""

from __future__ import annotations

from typing import Any

from ..cardkit.builder import (
    _LOADING_ELEMENT_ID,
    _build_reasoning_panel,
    _build_tool_panel,
    _format_elapsed,
    _streaming_element,
)
from ..cardkit.i18n import _T, _i18n
from .segments import Segment, SegmentType
from .tooluse import ToolDisplayStep

ELEMENT_THRESHOLD = 180  # 飞书硬上限 200，预留 20 给 footer + 波动
FOOTER_RESERVE = 2  # footer 元素预留（hr + markdown）


def estimate_segment_elements(seg: Segment, all_steps: list[ToolDisplayStep]) -> int:
    """估算单个 segment 新增的卡片元素数."""
    if seg.type == SegmentType.REASONING:
        return 4  # collapsible_panel + plain_text + standard_icon + markdown
    if seg.type == SegmentType.ANSWER:
        return 1
    if seg.type == SegmentType.TOOL:
        return estimate_tool_elements(
            seg.tool_offset,
            tool_segment_end(seg, all_steps),
            all_steps,
        )
    return 0


def tool_segment_end(seg: Segment, all_steps: list[ToolDisplayStep]) -> int:
    return seg.tool_end_offset if seg.tool_end_offset else len(all_steps)


def estimate_tool_elements(start: int, end: int, all_steps: list[ToolDisplayStep]) -> int:
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


def find_tool_split_offset(
    *,
    base_count: int,
    seg: Segment,
    all_steps: list[ToolDisplayStep],
) -> int | None:
    """寻找 tool step 拆分点，让当前卡保留尽可能多的 steps."""
    start = seg.tool_offset
    end = tool_segment_end(seg, all_steps)
    if end - start <= 1:
        return None
    for split_offset in range(end - 1, start, -1):
        estimate = estimate_tool_elements(start, split_offset, all_steps)
        if base_count + estimate + FOOTER_RESERVE <= ELEMENT_THRESHOLD:
            return split_offset
    return None


def build_add_segment_action(seg: Segment, all_steps: list[ToolDisplayStep]) -> dict[str, Any]:
    """构造新增 segment 元素的 batch action."""
    if seg.type == SegmentType.REASONING:
        element = _build_reasoning_panel(
            " ",
            seg.elapsed_ms,
            expanded=True,
            element_id=seg.el_id,
            text_element_id=seg.text_el_id,
        )
    elif seg.type == SegmentType.ANSWER:
        element = _streaming_element(element_id=seg.el_id)
    elif seg.type == SegmentType.TOOL:
        start = seg.tool_offset
        end = seg.tool_end_offset if seg.tool_end_offset else len(all_steps)
        element = _build_tool_panel(all_steps[start:end], element_id=seg.el_id)
    else:
        raise ValueError(f"unsupported segment type: {seg.type}")

    return {
        "action": "add_elements",
        "params": {
            "type": "insert_before",
            "target_element_id": _LOADING_ELEMENT_ID,
            "elements": [element],
        },
    }


def build_reasoning_finalized_action(seg: Segment) -> dict[str, Any]:
    """构造 reasoning header 耗时终结 action."""
    elapsed = _format_elapsed(seg.elapsed_ms)
    en_label = _T["thought_for"][0].format(elapsed)
    zh_label = _T["thought_for"][1].format(elapsed)
    return {
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
    }


def build_tool_update_action(
    *,
    element_id: str,
    steps: list[ToolDisplayStep],
) -> dict[str, Any]:
    """构造 tool panel 局部更新 action."""
    panel = _build_tool_panel(steps)
    return {
        "action": "partial_update_element",
        "params": {
            "element_id": element_id,
            "partial_element": {
                "elements": panel["elements"],
                "header": panel["header"],
            },
        },
    }
