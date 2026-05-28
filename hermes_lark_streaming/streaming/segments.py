"""流式卡片状态追踪 — 扁平 segments 管理."""

from __future__ import annotations

import time
from enum import StrEnum


class SegmentType(StrEnum):
    REASONING = "reasoning"
    ANSWER = "answer"
    TOOL = "tool"


class Segment:
    """单个内容段 — reasoning / answer / tool."""

    __slots__ = (
        "created",
        "dirty",
        "el_id",
        "elapsed_ms",
        "element_estimate",
        "reasoning_finalized",
        "start_time",
        "text",
        "text_el_id",
        "tool_end_offset",
        "tool_offset",
        "type",
    )

    def __init__(self, seg_type: SegmentType | str, el_id: str) -> None:
        self.type = SegmentType(seg_type)
        self.el_id = el_id
        self.created = False
        self.dirty = True
        self.element_estimate: int = 0
        self.text: str = ""
        self.text_el_id: str = ""
        self.tool_offset: int = 0
        self.tool_end_offset: int = 0  # 0 = 未终结；>= 1 表示已终结
        self.start_time: float = 0.0
        self.elapsed_ms: float = 0.0
        self.reasoning_finalized: bool = False


class SegmentState:
    """管理单张流式卡片的扁平内容段列表.

    纯数据类，不含 IO。每个 segment 是一个内容块（reasoning/answer/tool），
    按事件到达顺序排列，无需推断轮次边界。
    """

    __slots__ = (
        "_counter",
        "segments",
    )

    def __init__(self) -> None:
        self._counter = 0
        self.segments: list[Segment] = []

    def _new_reasoning(self, text: str) -> Segment:
        c = self._counter
        self._counter += 1
        seg = Segment(SegmentType.REASONING, f"reasoning_{c}_panel")
        seg.text_el_id = f"reasoning_{c}_text"
        seg.text = text
        seg.start_time = time.time()
        self.segments.append(seg)
        return seg

    def _new_answer(self, text: str) -> Segment:
        c = self._counter
        self._counter += 1
        seg = Segment(SegmentType.ANSWER, f"answer_{c}")
        seg.text = text
        seg.start_time = time.time()
        self._finalize_prev_reasoning(seg.start_time)
        self.segments.append(seg)
        return seg

    def _new_tool(self, tool_offset: int) -> Segment:
        c = self._counter
        self._counter += 1
        seg = Segment(SegmentType.TOOL, f"tools_{c}")
        seg.tool_offset = tool_offset
        seg.start_time = time.time()
        self._finalize_prev_reasoning(seg.start_time)
        self.segments.append(seg)
        return seg

    def _finalize_prev_reasoning(self, now: float) -> None:
        """终结最后一个未计算耗时的 reasoning segment."""
        for seg in reversed(self.segments):
            if seg.type == SegmentType.REASONING and seg.start_time and not seg.elapsed_ms:
                seg.elapsed_ms = (now - seg.start_time) * 1000
                break

    def on_reasoning_delta(self, text: str) -> None:
        """处理 reasoning 增量，同类型追加否则新建 segment."""
        if self.segments and self.segments[-1].type == SegmentType.REASONING:
            self.segments[-1].text += text
            self.segments[-1].dirty = True
        else:
            self._new_reasoning(text)

    def on_answer_delta(self, text: str) -> None:
        """处理 answer 增量，同类型追加否则新建 segment."""
        if self.segments and self.segments[-1].type == SegmentType.ANSWER:
            self.segments[-1].text += text
            self.segments[-1].dirty = True
        else:
            self._new_answer(text)

    def on_tool_event(self, tool_step_count: int) -> None:
        """处理工具调用事件，同类型标记 dirty 否则新建 segment 并终结前序 tool segment."""
        if tool_step_count <= 0:
            return
        if self.segments and self.segments[-1].type == SegmentType.TOOL:
            self.segments[-1].dirty = True
            return
        for seg in reversed(self.segments):
            if seg.type == SegmentType.TOOL and seg.tool_end_offset == 0:
                seg.tool_end_offset = tool_step_count - 1
                seg.dirty = True
                break
        self._new_tool(tool_step_count - 1)

    def split_tool_segment(
        self,
        index: int,
        split_tool_offset: int,
    ) -> Segment:
        """在 step 边界拆分一个 tool segment，返回承接后续 steps 的新 segment."""
        seg = self.segments[index]
        c = self._counter
        self._counter += 1
        new_seg = Segment(SegmentType.TOOL, f"tools_{c}")
        new_seg.tool_offset = split_tool_offset
        new_seg.tool_end_offset = seg.tool_end_offset
        new_seg.start_time = seg.start_time
        seg.tool_end_offset = split_tool_offset
        seg.dirty = True
        self.segments.insert(index + 1, new_seg)
        return new_seg

    def finalize_segments(self, total_tool_count: int) -> None:
        """完成态调用：终结最后一个 tool segment + 补算最后一个 reasoning elapsed_ms."""
        now = time.time()
        for seg in reversed(self.segments):
            if seg.type == SegmentType.TOOL and seg.tool_end_offset == 0:
                seg.tool_end_offset = total_tool_count
                break

        for seg in reversed(self.segments):
            if seg.type == SegmentType.REASONING and seg.start_time and not seg.elapsed_ms:
                seg.elapsed_ms = (now - seg.start_time) * 1000
                break

    @property
    def has_dirty(self) -> bool:
        """是否有需要 flush 的脏段."""
        return any(seg.dirty for seg in self.segments)
