"""cardkit 包测试 — markdown 优化、表格处理、卡片构建."""

from __future__ import annotations

from hermes_lark_streaming.cardkit.builder import (
    REASONING_ELEMENT_ID,
    REASONING_TEXT_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_footer_elements,
    _build_header,
    _build_reasoning_panel,
    _build_tool_panel,
    _compact,
    _escape_md,
    _format_elapsed,
    _longest_backtick_run,
    build_complete_card,
    build_streaming_card_v2,
)
from hermes_lark_streaming.cardkit.markdown import (
    _downgrade_tables,
    _find_tables_outside_code_blocks,
    _split_long_text,
    _strip_invalid_image_keys,
    optimize_markdown_style,
)
from hermes_lark_streaming.streaming.segments import Segment

# --- Markdown 优化 ---


class TestOptimizeMarkdownStyle:
    def test_h1_downgraded_to_h4(self) -> None:
        assert "#### Title" in optimize_markdown_style("# Title")

    def test_h2_downgraded_to_h5(self) -> None:
        assert "##### Sub" in optimize_markdown_style("## Sub")

    def test_h3_downgraded_to_h5(self) -> None:
        assert "##### Deep" in optimize_markdown_style("### Deep")

    def test_h4_h5_h6_unchanged(self) -> None:
        text = "#### H4\n##### H5\n###### H6"
        result = optimize_markdown_style(text)
        assert "#### H4" in result
        assert "##### H5" in result

    def test_heading_in_code_block_preserved(self) -> None:
        text = "```\n# Should not change\n```"
        assert "# Should not change" in optimize_markdown_style(text)

    def test_blank_line_compression(self) -> None:
        result = optimize_markdown_style("a\n\n\n\n\nb")
        assert "\n\n\n" not in result

    def test_invalid_image_key_removed(self) -> None:
        text = "![alt](not_img_key)"
        assert "not_img_key" not in optimize_markdown_style(text)

    def test_valid_img_key_preserved(self) -> None:
        text = "![alt](img_v3_abc123)"
        assert "img_v3_abc123" in optimize_markdown_style(text)

    def test_no_headings_unchanged(self) -> None:
        text = "plain text\nanother line"
        assert optimize_markdown_style(text) == text

    def test_mixed_headings_and_code(self) -> None:
        text = "# Title\n```\n# Code heading\n```\n## Sub"
        result = optimize_markdown_style(text)
        assert "#### Title" in result
        assert "# Code heading" in result


class TestStripInvalidImageKeys:
    def test_no_images_unchanged(self) -> None:
        assert _strip_invalid_image_keys("no images") == "no images"

    def test_img_prefix_kept(self) -> None:
        assert "img_v3_test" in _strip_invalid_image_keys("![a](img_v3_test)")

    def test_non_img_removed(self) -> None:
        assert "http://example.com/img.png" not in _strip_invalid_image_keys("![a](http://example.com/img.png)")


# --- 表格处理 ---


class TestFindTablesOutsideCodeBlocks:
    def test_no_tables(self) -> None:
        assert _find_tables_outside_code_blocks("no tables here") == []

    def test_single_table(self) -> None:
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1

    def test_table_inside_code_block_ignored(self) -> None:
        text = "```\n| A | B |\n|---|---|\n| 1 | 2 |\n```"
        assert _find_tables_outside_code_blocks(text) == []

    def test_mixed(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n```\n{table}\n```"
        results = _find_tables_outside_code_blocks(text)
        assert len(results) == 1


class TestDowngradeTables:
    def test_within_limit_unchanged(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = f"{table}\n\n{table}\n\n{table}"
        assert _downgrade_tables(text) == text

    def test_over_limit_downgraded(self) -> None:
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        text = "\n\n".join([table] * 6)
        result = _downgrade_tables(text)
        assert result.count("```") >= 2  # 超限表格被包装为代码块


# --- 文本拆分 ---


class TestSplitLongText:
    def test_short_text_not_split(self) -> None:
        assert _split_long_text("short") == ["short"]

    def test_long_text_split_at_paragraph(self) -> None:
        chunk = "x" * 1200
        text = f"{chunk}\n\n{chunk}\n\n{chunk}"
        parts = _split_long_text(text, limit=2000)
        assert len(parts) > 1

    def test_no_paragraph_break_falls_back_to_newline(self) -> None:
        lines = ["word " * 100 for _ in range(30)]
        text = "\n".join(lines)
        parts = _split_long_text(text, limit=500)
        assert len(parts) > 1

    def test_exact_limit_not_split(self) -> None:
        text = "a" * 2400
        assert len(_split_long_text(text)) == 1


# --- 工具面板 ---

_STEP_RUNNING = {
    "name": "read",
    "title": "Read",
    "status": "running",
    "detail": "",
    "output": "",
    "error": "",
    "icon": "icon",
    "elapsed_ms": 0,
    "result_block": None,
    "error_block": None,
}
_STEP_SUCCESS = {**_STEP_RUNNING, "status": "success", "output": "ok", "elapsed_ms": 100}


class TestBuildToolPanel:
    def test_empty_steps(self) -> None:
        panel = _build_tool_panel([])
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID
        assert "Tool use" in panel["header"]["title"]["content"]

    def test_with_steps(self) -> None:
        panel = _build_tool_panel([_STEP_SUCCESS], elapsed_ms=500)
        assert panel["element_id"] == TOOL_PANEL_ELEMENT_ID

    def test_with_elapsed(self) -> None:
        panel = _build_tool_panel([_STEP_RUNNING], elapsed_ms=3000)
        title = panel["header"]["title"]["content"]
        assert "3.0s" in title


# --- Footer ---


class TestBuildFooterElements:
    def test_empty_data_renders_default_status(self) -> None:
        # 默认字段包含 "status"，总是会渲染
        result = _build_footer_elements({})
        assert len(result) >= 2
        assert "Completed" in result[1]["content"]

    def test_status_completed(self) -> None:
        result = _build_footer_elements({"duration": 5})
        assert len(result) >= 2  # hr + markdown 元素
        assert "Completed" in result[1]["content"]

    def test_status_error(self) -> None:
        result = _build_footer_elements({}, is_error=True)
        assert "red" in result[1]["content"]

    def test_status_aborted(self) -> None:
        result = _build_footer_elements({}, is_aborted=True)
        assert "Stopped" in result[1]["content"]

    def test_elapsed_displayed(self) -> None:
        result = _build_footer_elements({"duration": 12.5}, fields=[["elapsed"]])
        assert "12.5s" in result[1]["content"]

    def test_model_displayed(self) -> None:
        result = _build_footer_elements({"model": "claude-3"}, fields=[["model"]])
        assert "claude-3" in result[1]["content"]

    def test_context_displayed(self) -> None:
        result = _build_footer_elements(
            {"context_used": 50000, "context_max": 200000},
            fields=[["context"]],
        )
        assert "50.0K" in result[1]["content"]
        assert "25%" in result[1]["content"]

    def test_tokens_displayed(self) -> None:
        result = _build_footer_elements(
            {"input_tokens": 1000, "output_tokens": 500},
            fields=[["tokens"]],
        )
        assert "↑" in result[1]["content"]
        assert "↓" in result[1]["content"]

    def test_show_label(self) -> None:
        result = _build_footer_elements(
            {"duration": 5},
            fields=[["elapsed"]],
            show_label=True,
        )
        assert "Elapsed" in result[1]["content"]

    def test_multi_row_fields(self) -> None:
        result = _build_footer_elements(
            {"duration": 5, "model": "gpt"},
            fields=[["elapsed"], ["model"]],
        )
        assert "\n" in result[1]["content"]

    def test_none_footer_data_renders_status(self) -> None:
        result = _build_footer_elements(None)
        assert len(result) >= 2

    def test_no_matching_fields(self) -> None:
        assert _build_footer_elements({}, fields=[["tokens"]]) == []


# --- 推理面板 ---


class TestBuildReasoningPanel:
    def test_without_elapsed(self) -> None:
        panel = _build_reasoning_panel("thinking content")
        assert "Thought" in panel["header"]["title"]["content"]
        assert not panel["expanded"]

    def test_with_elapsed(self) -> None:
        panel = _build_reasoning_panel("thoughts", elapsed_ms=5000)
        title = panel["header"]["title"]["content"]
        assert "5.0s" in title

    def test_expanded_true(self) -> None:
        panel = _build_reasoning_panel("text", expanded=True)
        assert panel["expanded"] is True

    def test_expanded_default_false(self) -> None:
        panel = _build_reasoning_panel("text")
        assert panel["expanded"] is False

    def test_element_id_set(self) -> None:
        panel = _build_reasoning_panel("text", element_id=REASONING_ELEMENT_ID)
        assert panel["element_id"] == REASONING_ELEMENT_ID

    def test_element_id_default_none(self) -> None:
        panel = _build_reasoning_panel("text")
        assert "element_id" not in panel

    def test_inner_markdown_has_element_id(self) -> None:
        panel = _build_reasoning_panel("text")
        inner = panel["elements"][0]
        assert inner["element_id"] == REASONING_TEXT_ELEMENT_ID

    def test_title_is_plain_text_grey(self) -> None:
        panel = _build_reasoning_panel("text")
        title = panel["header"]["title"]
        assert title["tag"] == "plain_text"
        assert title["text_color"] == "grey"
        assert title["text_size"] == "notation"

    def test_empty_text_shows_thinking_title(self) -> None:
        panel = _build_reasoning_panel(" ")
        assert "Thinking" in panel["header"]["title"]["content"]

    def test_empty_string_shows_thinking_title(self) -> None:
        panel = _build_reasoning_panel("")
        assert "Thinking" in panel["header"]["title"]["content"]

    def test_with_content_shows_thought_title(self) -> None:
        panel = _build_reasoning_panel("reasoning here")
        assert "Thought" in panel["header"]["title"]["content"]
        assert "Thinking" not in panel["header"]["title"]["content"]


# --- 数字格式化 ---


class TestCompact:
    def test_small_number(self) -> None:
        assert _compact(42) == "42"

    def test_thousands(self) -> None:
        assert _compact(1500) == "1.5K"

    def test_millions(self) -> None:
        assert _compact(2_500_000) == "2.5M"

    def test_exact_thousand(self) -> None:
        assert _compact(1000) == "1.0K"

    def test_large_millions(self) -> None:
        assert _compact(250_000_000) == "250M"


class TestFormatElapsed:
    def test_sub_minute(self) -> None:
        assert _format_elapsed(3500) == "3.5s"

    def test_over_minute(self) -> None:
        assert _format_elapsed(125_000) == "2m 5s"

    def test_exactly_one_minute(self) -> None:
        assert _format_elapsed(60_000) == "1m 0s"


# --- 工具函数 ---


class TestEscapeMd:
    def test_escapes_special_chars(self) -> None:
        result = _escape_md("a`b*c{d}e[f]g<h>i")
        assert "\\" in result

    def test_plain_text_unchanged(self) -> None:
        assert _escape_md("hello world") == "hello world"


class TestLongestBacktickRun:
    def test_no_backticks(self) -> None:
        assert _longest_backtick_run("no backticks") == 0

    def test_single(self) -> None:
        assert _longest_backtick_run("a `b` c") == 1

    def test_triple(self) -> None:
        assert _longest_backtick_run("```code```") == 3


# --- 完整卡片构建 ---


class TestBuildStreamingCardV2:
    def test_structure(self) -> None:
        card = build_streaming_card_v2()
        assert card["schema"] == "2.0"
        assert card["config"]["streaming_mode"] is True
        assert card["body"]["elements"]

    def test_with_tool_steps(self) -> None:
        card = build_streaming_card_v2(tool_steps=[_STEP_RUNNING], elapsed_ms=100)
        assert any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_no_tool_use(self) -> None:
        card = build_streaming_card_v2(show_tool_use=False)
        assert not any(e.get("element_id") == TOOL_PANEL_ELEMENT_ID for e in card["body"]["elements"])

    def test_show_reasoning_adds_panel(self) -> None:
        card = build_streaming_card_v2(show_reasoning=True)
        assert any(e.get("element_id") == REASONING_ELEMENT_ID for e in card["body"]["elements"])

    def test_show_reasoning_default_no_panel(self) -> None:
        card = build_streaming_card_v2()
        assert not any(e.get("element_id") == REASONING_ELEMENT_ID for e in card["body"]["elements"])

    def test_reasoning_before_tool_before_answer(self) -> None:
        card = build_streaming_card_v2(
            show_reasoning=True,
            tool_steps=[_STEP_RUNNING],
            elapsed_ms=100,
            show_tool_use=True,
        )
        ids = [e.get("element_id") for e in card["body"]["elements"]]
        reasoning_idx = ids.index(REASONING_ELEMENT_ID)
        tool_idx = ids.index(TOOL_PANEL_ELEMENT_ID)
        assert reasoning_idx < tool_idx

    def test_reasoning_panel_expanded(self) -> None:
        card = build_streaming_card_v2(show_reasoning=True)
        panel = next(e for e in card["body"]["elements"] if e.get("element_id") == REASONING_ELEMENT_ID)
        assert panel["expanded"] is True


# --- 分段完成态卡片 ---


def _seg(seg_type: str, text: str = "", **kwargs: int | float) -> Segment:
    """创建测试用 Segment mock."""
    seg = Segment(seg_type, f"{seg_type}_0")
    seg.text = text
    if seg_type == "reasoning":
        seg.text_el_id = f"{seg_type}_0_text"
    seg.tool_offset = int(kwargs.get("tool_offset", 0))
    seg.tool_end_offset = int(kwargs.get("tool_end_offset", 0))
    seg.elapsed_ms = float(kwargs.get("elapsed_ms", 0.0))
    seg.start_time = float(kwargs.get("start_time", 0.0))
    seg.created = True
    seg.dirty = False
    return seg


class TestBuildSegmentCompleteCard:
    def test_empty_segments_and_skipped_reasoning(self) -> None:
        """空 segments 渲染 Done；空 reasoning 被跳过."""
        card = build_complete_card(segments=[], all_tool_steps=[])
        assert card["schema"] == "2.0"
        assert any("Done" in str(e) or "完成" in str(e) for e in card["body"]["elements"])

        card2 = build_complete_card(segments=[_seg("reasoning", "")], all_tool_steps=[])
        assert any("Done" in str(e) or "完成" in str(e) for e in card2["body"]["elements"])

    def test_answer_only_no_done(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hello world")],
            all_tool_steps=[],
        )
        elements = card["body"]["elements"]
        assert any("hello world" in str(e) for e in elements)
        assert not any("Done" in str(e) for e in elements)

    def test_reasoning_before_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("reasoning", "think"), _seg("answer", "reply")],
            all_tool_steps=[],
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r_idx = next(i for i, c in enumerate(contents) if "think" in c)
        a_idx = next(i for i, c in enumerate(contents) if "reply" in c)
        assert r_idx < a_idx

    def test_tool_segment_uses_steps_slice(self) -> None:
        steps = [_STEP_RUNNING, _STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=1, tool_end_offset=3)],
            all_tool_steps=steps,
        )
        tool_elements = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
        assert len(tool_elements) == 1
        assert len(tool_elements[0].get("elements", [])) == 2  # steps[1:3]

    def test_three_round_ordering(self) -> None:
        card = build_complete_card(
            segments=[
                _seg("reasoning", "r1"),
                _seg("answer", "a1"),
                _seg("tool", tool_offset=0, tool_end_offset=2),
                _seg("reasoning", "r2"),
                _seg("answer", "a2"),
            ],
            all_tool_steps=[_STEP_SUCCESS, _STEP_RUNNING],
            merge_segments=False,
        )
        contents = [str(e) for e in card["body"]["elements"]]
        r1 = next(i for i, c in enumerate(contents) if "r1" in c)
        a1 = next(i for i, c in enumerate(contents) if "a1" in c)
        r2 = next(i for i, c in enumerate(contents) if "r2" in c)
        a2 = next(i for i, c in enumerate(contents) if "a2" in c)
        assert r1 < a1 < r2 < a2

    def test_tool_end_offset_zero_uses_all_steps(self) -> None:
        steps = [_STEP_SUCCESS, _STEP_RUNNING]
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=0, tool_end_offset=0)],
            all_tool_steps=steps,
        )
        inner = next(e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel")["elements"]
        assert len(inner) == 2

    def test_tool_empty_steps_skipped(self) -> None:
        card = build_complete_card(
            segments=[_seg("tool", tool_offset=5, tool_end_offset=5)],
            all_tool_steps=[_STEP_SUCCESS],
        )
        assert not any(e.get("tag") == "collapsible_panel" for e in card["body"]["elements"])

    def test_summary_truncated_from_last_answer(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "short"), _seg("answer", "x" * 200)],
            all_tool_steps=[],
        )
        summary = card["config"].get("summary", {}).get("content", "")
        assert len(summary) <= 120


class TestBuildCronCard:
    def test_basic_card_structure(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Hello **world**")
        assert card["schema"] == "2.0"
        assert card["body"]["elements"][0]["tag"] == "markdown"
        assert "Hello **world**" in card["body"]["elements"][0]["content"]

    def test_summary_from_content(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("Line 1\nLine 2\n" + "x" * 200)
        summary = card["config"]["summary"]["content"]
        assert summary.startswith("Line 1 Line 2")
        assert len(summary) <= 120

    def test_empty_content(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        card = build_cron_card("")
        assert card["body"]["elements"] == []

    def test_table_content_preserved(self) -> None:
        from hermes_lark_streaming.cardkit.builder import build_cron_card

        content = "| A | B |\n|---|---|\n| 1 | 2 |"
        card = build_cron_card(content)
        assert "| A | B |" in card["body"]["elements"][0]["content"]


# --- Header ---


class TestBuildHeader:
    def test_streaming_blue(self) -> None:
        header = _build_header("streaming")
        assert header is not None
        assert header["template"] == "blue"
        assert "Processing" in header["title"]["content"]

    def test_completed_green(self) -> None:
        header = _build_header("completed")
        assert header is not None
        assert header["template"] == "green"
        assert "Completed" in header["title"]["content"]

    def test_error_red(self) -> None:
        header = _build_header("error")
        assert header is not None
        assert header["template"] == "red"
        assert "Error" in header["title"]["content"]

    def test_stopped_red(self) -> None:
        header = _build_header("stopped")
        assert header is not None
        assert header["template"] == "red"
        assert "Stopped" in header["title"]["content"]

    def test_title_has_i18n(self) -> None:
        header = _build_header("streaming")
        assert "i18n_content" in header["title"]
        assert "zh_cn" in header["title"]["i18n_content"]
        assert "en_us" in header["title"]["i18n_content"]

    def test_unknown_status_falls_back_to_completed(self) -> None:
        header = _build_header("unknown")
        assert header is not None
        assert header["template"] == "green"
        assert "Completed" in header["title"]["content"]


class TestStreamingCardHeader:
    def test_header_absent_by_default(self) -> None:
        card = build_streaming_card_v2()
        assert "header" not in card

    def test_header_present_when_enabled(self) -> None:
        card = build_streaming_card_v2(header_enabled=True)
        assert "header" in card
        assert card["header"]["template"] == "blue"


class TestCompleteCardHeader:
    def test_completed_has_green_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "green"

    def test_aborted_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_aborted=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"

    def test_error_has_red_header(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            is_error=True,
            header_enabled=True,
        )
        assert "header" in card
        assert card["header"]["template"] == "red"
        assert "Error" in card["header"]["title"]["content"]

    def test_header_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            header_enabled=False,
        )
        assert "header" not in card


class TestCompleteCardFooter:
    def test_footer_present_by_default(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" in tags

    def test_footer_disabled(self) -> None:
        card = build_complete_card(
            segments=[_seg("answer", "hi")],
            all_tool_steps=[],
            footer_enabled=False,
        )
        tags = [e.get("tag") for e in card["body"]["elements"]]
        assert "hr" not in tags


class TestCompleteCardMergeSegments:
    """测试 build_complete_card 的 segment 合并行为."""

    def _panel_titles(self, card: dict) -> list[str]:
        """提取所有 collapsible_panel 的标题文本."""
        titles: list[str] = []
        for e in card["body"]["elements"]:
            if e.get("tag") != "collapsible_panel":
                continue
            title = e.get("header", {}).get("title", {}).get("content", "")
            titles.append(title)
        return titles

    def test_merge_disabled_keeps_separate_panels(self) -> None:
        """merge_segments=False 时保持原行为 — 每个段一个 panel."""
        segs = [
            _seg("reasoning", "think 1", elapsed_ms=1000),
            _seg("reasoning", "think 2", elapsed_ms=2000),
            _seg("answer", "result"),
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=[], merge_segments=False,
        )
        titles = self._panel_titles(card)
        # 应该有 2 个 thinking panel
        assert sum(1 for t in titles if "Thought" in t or "思考了" in t) == 2

    def test_merge_enabled_combines_reasoning(self) -> None:
        """merge_segments=True + 段数 > 阈值时合并."""
        segs = [
            _seg("reasoning", "first thought", elapsed_ms=1000),
            _seg("reasoning", "second thought", elapsed_ms=2000),
            _seg("answer", "final answer"),
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=[], merge_segments=True, merge_threshold=1,
        )
        titles = self._panel_titles(card)
        # 2 段 thinking 应该合并成 1 个 panel
        think_titles = [t for t in titles if "Thought" in t or "思考了" in t]
        assert len(think_titles) == 1
        # panel 元素应该包含两段内容（用 --- 分隔）
        # 找出 reasoning panel（用 text 元素包含 "first thought" 的 panel）
        for e in card["body"]["elements"]:
            if e.get("tag") != "collapsible_panel":
                continue
            for sub in e.get("elements", []):
                if sub.get("tag") == "markdown" and "first thought" in sub.get("content", ""):
                    content = sub["content"]
                    assert "second thought" in content
                    assert "---" in content
                    return
        raise AssertionError("未找到合并后的 reasoning panel")

    def test_merge_tool_segments(self) -> None:
        """多个 TOOL 段合并成 1 个 panel，steps 范围是 union."""
        steps = [
            {"name": "a", "title": "A", "status": "success"},
            {"name": "b", "title": "B", "status": "success"},
            {"name": "c", "title": "C", "status": "success"},
        ]
        segs = [
            _seg("tool", tool_offset=0, tool_end_offset=1),
            _seg("tool", tool_offset=2, tool_end_offset=3),
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=steps,
            merge_segments=True, merge_threshold=1,
        )
        # 应该只有 1 个 tool panel
        tool_titles = [t for t in self._panel_titles(card) if "Tool" in t or "工具执行" in t or "工具使用" in t]
        assert len(tool_titles) == 1
        # 2 步合并（A 和 C）
        for e in card["body"]["elements"]:
            if e.get("tag") != "collapsible_panel":
                continue
            title = e.get("header", {}).get("title", {}).get("content", "")
            if "Tool" not in title and "工具执行" not in title and "工具使用" not in title:
                continue
            # 应该有 2 步（A, C），不是 3
            sub_titles = [
                s.get("text", {}).get("content", "")
                for s in e.get("elements", [])
                if s.get("tag") == "div"
            ]
            assert len(sub_titles) == 2
            return
        raise AssertionError("未找到 tool panel")

    def test_merge_threshold_suppresses_short_runs(self) -> None:
        """merge_threshold=5 时，2 段不合并（≤ 阈值）."""
        segs = [
            _seg("reasoning", "only one", elapsed_ms=1000),
            _seg("reasoning", "second", elapsed_ms=2000),
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=[],
            merge_segments=True, merge_threshold=5,
        )
        titles = self._panel_titles(card)
        # 2 段 ≤ 5，不合并 — 应该有 2 个独立 panel
        think_titles = [t for t in titles if "Thought" in t or "思考了" in t]
        assert len(think_titles) == 2

    def test_mixed_reasoning_and_tool_all_merged(self) -> None:
        """混合场景：3 段 thinking + 2 个 tool 段 + answer → 2 个 panel + answer."""
        segs = [
            _seg("reasoning", "t1", elapsed_ms=500),
            _seg("tool", tool_offset=0, tool_end_offset=1),
            _seg("reasoning", "t2", elapsed_ms=800),
            _seg("tool", tool_offset=1, tool_end_offset=2),
            _seg("reasoning", "t3", elapsed_ms=300),
            _seg("answer", "final"),
        ]
        steps = [
            {"name": "x", "title": "X", "status": "success"},
            {"name": "y", "title": "Y", "status": "success"},
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=steps,
            merge_segments=True, merge_threshold=1,
        )
        titles = self._panel_titles(card)
        # 1 个 reasoning + 1 个 tool = 2 个 panel
        think_count = sum(1 for t in titles if "Thought" in t or "思考了" in t)
        tool_count = sum(1 for t in titles if "Tool" in t or "工具执行" in t or "工具使用" in t)
        assert think_count == 1
        assert tool_count == 1
        # answer 元素存在
        assert any("final" in str(e) for e in card["body"]["elements"])


class TestReasoningPanelStepCount:
    """验证 reasoning panel 标题在合并时显示步数."""

    def _reasoning_title(self, card: dict) -> tuple[str, str]:
        """提取 reasoning panel 的 en/zh 标题."""
        for e in card["body"]["elements"]:
            if e.get("tag") != "collapsible_panel":
                continue
            title = e.get("header", {}).get("title", {})
            content = title.get("content", "")
            if "Thought" in content or "思考" in content:
                i18n = title.get("i18n_content", {})
                return content, i18n.get("zh_cn", "")
        return "", ""

    def test_merged_reasoning_shows_step_count(self) -> None:
        """3 段 thinking 合并时标题应显示 '3 steps' / '3 步'."""
        segs = [
            _seg("reasoning", "t1", elapsed_ms=1000),
            _seg("reasoning", "t2", elapsed_ms=2000),
            _seg("reasoning", "t3", elapsed_ms=3000),
            _seg("answer", "done"),
        ]
        card = build_complete_card(
            segments=segs, all_tool_steps=[],
            merge_segments=True, merge_threshold=1,
        )
        en, zh = self._reasoning_title(card)
        assert "3 steps" in en, f"expected '3 steps' in: {en}"
        assert "3 步" in zh, f"expected '3 步' in: {zh}"
        # elapsed 取最大（hermes 现有语义）
        assert "3.0s" in en, f"expected max elapsed 3.0s in: {en}"
        assert "3.0s" in zh, f"expected 3.0s in: {zh}"

    def test_single_reasoning_hides_step_count(self) -> None:
        """1 段 reasoning 时不显示步数（保持原行为）."""
        card = build_complete_card(
            segments=[_seg("reasoning", "solo", elapsed_ms=2500), _seg("answer", "ok")],
            all_tool_steps=[], merge_segments=True, merge_threshold=1,
        )
        en, zh = self._reasoning_title(card)
        assert "step" not in en.lower(), f"single reasoning should not show step: {en}"
        assert "步" not in zh, f"single reasoning should not show 步: {zh}"
        assert "2.5s" in en and "2.5s" in zh

    def test_tool_panel_uses_singular_for_one_step(self) -> None:
        """回归测试：单步 tool 标题 '1 step' / '1 步'（不被影响）."""
        card = build_complete_card(
            segments=[
                _seg("tool", tool_offset=0, tool_end_offset=1),
                _seg("answer", "ok"),
            ],
            all_tool_steps=[{"name": "x", "title": "X", "status": "success"}],
            merge_segments=True, merge_threshold=1,
        )
        for e in card["body"]["elements"]:
            if e.get("tag") != "collapsible_panel":
                continue
            title = e.get("header", {}).get("title", {})
            content = title.get("content", "")
            if "Tool" in content:
                assert "1 step" in content, f"single tool step: {content}"
