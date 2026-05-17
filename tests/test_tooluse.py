"""tooluse.py 测试 — 脱敏、清洗、显示块构建、工具调用追踪."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from hermes_lark_streaming.tooluse import (
    ToolUseTracker,
    _basename_only,
    _build_display_block,
    _fenced_block,
    _format_duration_label,
    _humanize_tool_name,
    _resolve_tool_descriptor,
    _sanitize_detail,
    redact_inline_secrets,
)


class TestRedactInlineSecrets:
    def test_sensitive_key_value(self) -> None:
        assert "token=xxx" not in redact_inline_secrets("token=xxx")
        assert "token=[redacted]" in redact_inline_secrets("token=xxx")

    def test_sensitive_key_quoted_value(self) -> None:
        result = redact_inline_secrets('password="my secret"')
        assert "my secret" not in result
        assert "password=[redacted]" in result

    def test_sensitive_key_single_quoted(self) -> None:
        result = redact_inline_secrets("api_key='secret'")
        assert "secret" not in result

    def test_nonsensitive_key_unchanged(self) -> None:
        text = "name=value count=42"
        assert redact_inline_secrets(text) == text

    def test_authorization_bearer(self) -> None:
        result = redact_inline_secrets("Authorization: Bearer sk-abc123")
        assert "sk-abc123" not in result
        assert "[redacted]" in result

    def test_authorization_basic(self) -> None:
        result = redact_inline_secrets("Authorization: Basic dXNlcjpwYXNz")
        assert "[redacted]" in result

    def test_flag_secret_redacted(self) -> None:
        result = redact_inline_secrets("--api-key mykey123")
        assert "[redacted]" in result

    def test_flag_equals_redacted(self) -> None:
        result = redact_inline_secrets("--secret=value")
        assert "[redacted]" in result

    def test_nonsensitive_flag_unchanged(self) -> None:
        text = "--output result.txt"
        assert redact_inline_secrets(text) == text

    def test_inline_assignment_and_header(self) -> None:
        result = redact_inline_secrets("token=abc Authorization: Bearer xyz")
        assert "abc" not in result
        assert "xyz" not in result


class TestSanitizeDetail:
    def test_empty_text(self) -> None:
        assert _sanitize_detail("", "command") == ""

    def test_none_sanitizer_passthrough(self) -> None:
        text = "<tag>content</tag>"
        assert _sanitize_detail(text, None) == text

    def test_command_sanitizer_strips_html(self) -> None:
        result = _sanitize_detail("<b>bold</b> text", "command")
        assert "<b>" not in result
        assert result == "bold text"

    def test_command_sanitizer_redacts_secrets(self) -> None:
        result = _sanitize_detail("run with token=secret", "command")
        assert "secret" not in result

    def test_path_sanitizer_basename_only(self) -> None:
        result = _sanitize_detail("/home/user/project/file.py", "path")
        assert result == "file.py"

    def test_path_sanitizer_strips_from_prefix(self) -> None:
        result = _sanitize_detail("from /some/deep/path.py", "path")
        assert result == "path.py"

    def test_search_sanitizer_strips_quotes(self) -> None:
        assert _sanitize_detail('"search term"', "search") == "search term"
        assert _sanitize_detail("'search term'", "search") == "search term"

    def test_url_sanitizer_strips_quotes(self) -> None:
        result = _sanitize_detail('"https://example.com"', "url")
        assert result == "https://example.com"

    def test_url_sanitizer_strips_from_prefix(self) -> None:
        result = _sanitize_detail("from https://example.com", "url")
        assert result == "https://example.com"

    def test_unknown_sanitizer_returns_cleaned(self) -> None:
        result = _sanitize_detail("<b>text</b>", "unknown_type")
        assert "<b>" not in result

    def test_empty_after_strip_returns_original(self) -> None:
        result = _sanitize_detail("<>", "command")
        assert result == "<>"


class TestBasenameOnly:
    def test_unix_path(self) -> None:
        assert _basename_only("/home/user/file.py") == "file.py"

    def test_windows_path(self) -> None:
        assert _basename_only("C:\\Users\\file.py") == "file.py"

    def test_trailing_slash(self) -> None:
        assert _basename_only("/home/user/dir/") == "dir"

    def test_empty_string(self) -> None:
        assert _basename_only("") == ""

    def test_just_filename(self) -> None:
        assert _basename_only("file.py") == "file.py"


class TestFormatDurationLabel:
    def test_milliseconds(self) -> None:
        assert _format_duration_label(500) == "500 ms"

    def test_exactly_one_second(self) -> None:
        assert _format_duration_label(1000) == "1.0 s"

    def test_seconds(self) -> None:
        assert _format_duration_label(2500) == "2.5 s"

    def test_zero(self) -> None:
        assert _format_duration_label(0) == "0 ms"


class TestResolveToolDescriptor:
    def test_known_tool(self) -> None:
        desc = _resolve_tool_descriptor("read")
        assert desc is not None
        assert desc["title"] == "Read"

    def test_alias_with_dash(self) -> None:
        desc = _resolve_tool_descriptor("web-search")
        assert desc is not None
        assert desc["title"] == "Search"

    def test_alias_with_underscore(self) -> None:
        desc = _resolve_tool_descriptor("web_search")
        assert desc is not None

    def test_unknown_tool_returns_none(self) -> None:
        assert _resolve_tool_descriptor("nonexistent_tool_xyz") is None

    def test_none_returns_none(self) -> None:
        assert _resolve_tool_descriptor(None) is None

    def test_prefix_match(self) -> None:
        desc = _resolve_tool_descriptor("read_file")
        assert desc is not None
        assert desc["title"] == "Read"


class TestHumanizeToolName:
    def test_dash_replaced(self) -> None:
        assert _humanize_tool_name("web-search") == "Web search"

    def test_underscore_replaced(self) -> None:
        assert _humanize_tool_name("web_search") == "Web search"

    def test_capitalized(self) -> None:
        assert _humanize_tool_name("read") == "Read"

    def test_empty_returns_tool(self) -> None:
        assert _humanize_tool_name("") == "Tool"

    def test_single_char(self) -> None:
        assert _humanize_tool_name("x") == "X"


class TestBuildDisplayBlock:
    def test_none_returns_none(self) -> None:
        assert _build_display_block(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _build_display_block("") is None

    def test_whitespace_string_returns_none(self) -> None:
        assert _build_display_block("   \n  ") is None

    def test_json_string_pretty_printed(self) -> None:
        result = _build_display_block('{"key": "value"}')
        assert result is not None
        assert result["language"] == "json"
        assert '"key"' in result["content"]

    def test_json_array_string(self) -> None:
        result = _build_display_block("[1, 2, 3]")
        assert result is not None
        assert result["language"] == "json"

    def test_invalid_json_as_text(self) -> None:
        result = _build_display_block("{invalid json")
        assert result is not None
        assert result["language"] == "text"

    def test_plain_text(self) -> None:
        result = _build_display_block("hello world")
        assert result is not None
        assert result["language"] == "text"
        assert result["content"] == "hello world"

    def test_command_sanitizer_redacts(self) -> None:
        result = _build_display_block("token=secret", sanitizer="command")
        assert "secret" not in result["content"]

    def test_dict_input(self) -> None:
        result = _build_display_block({"key": "value"})
        assert result is not None
        assert result["language"] == "json"

    def test_list_input(self) -> None:
        result = _build_display_block([1, 2, 3])
        assert result is not None
        assert result["language"] == "json"

    def test_fenced_block_has_fences(self) -> None:
        result = _build_display_block("some text")
        assert result is not None
        assert "```" in result["fenced"]


class TestFencedBlock:
    def test_basic_fence(self) -> None:
        result = _fenced_block("python", "print('hi')")
        assert result["language"] == "python"
        assert result["content"] == "print('hi')"
        assert result["fenced"].startswith("```python")

    def test_fence_uses_minimum_triple(self) -> None:
        # 内容中单反引号 → 最长连续 = 1, max(3, 1+1) = 3
        result = _fenced_block("text", "some `code` here")
        assert result["fenced"].startswith("```")

    def test_fence_escalates_for_triple_backticks(self) -> None:
        content = "```existing fence```"
        result = _fenced_block("text", content)
        assert result["fenced"].startswith("````")


class TestBuildDisplayStepsOffset:
    def test_offset_zero_returns_all(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        tracker.record_end("read", output="ok")
        tracker.record_start("exec", "cmd")
        tracker.record_end("exec", output="done")
        assert len(tracker.build_display_steps(offset=0)) == 2

    def test_offset_skips_first(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        tracker.record_end("read", output="ok")
        tracker.record_start("exec", "cmd")
        tracker.record_end("exec", output="done")
        steps = tracker.build_display_steps(offset=1)
        assert len(steps) == 1
        assert steps[0]["name"] == "exec"

    @pytest.mark.parametrize("offset", [2, 5])
    def test_offset_out_of_range_returns_empty(self, offset: int) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        tracker.record_end("read", output="ok")
        tracker.record_start("exec", "cmd")
        tracker.record_end("exec", output="done")
        assert tracker.build_display_steps(offset=offset) == []

    def test_no_session_returns_empty(self) -> None:
        assert ToolUseTracker().build_display_steps(offset=0) == []


class TestRecordEndReturnsIndex:
    def test_returns_step_index(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        assert tracker.record_end("read", output="ok") == 0

    def test_returns_none_without_session(self) -> None:
        assert ToolUseTracker().record_end("orphan", output="late") is None

    def test_no_matching_start_appends(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("other", "f.py")
        idx = tracker.record_end("read", output="late result")
        assert idx == 1

    def test_multiple_steps_return_correct_index(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        tracker.record_end("read", output="ok")
        tracker.record_start("exec", "cmd")
        assert tracker.record_end("exec", output="done") == 1

    def test_error_step_returns_index(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("exec", "cmd")
        assert tracker.record_end("exec", error="boom") == 0


class TestToolUseTracker:
    def test_empty_tracker(self) -> None:
        tracker = ToolUseTracker()
        assert tracker.build_display_steps() == []
        assert tracker.elapsed_ms == 0.0

    def test_record_start_creates_running_step(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "file.py")
        steps = tracker.build_display_steps()
        assert len(steps) == 1
        assert steps[0]["status"] == "running"
        assert steps[0]["name"] == "read"

    def test_record_end_matches_by_name(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "file.py")
        tracker.record_end("read", output="contents")
        steps = tracker.build_display_steps()
        assert steps[0]["status"] == "success"
        assert steps[0]["output"] == "contents"

    def test_record_end_with_error(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("exec", "bad command")
        tracker.record_end("exec", error="command failed")
        steps = tracker.build_display_steps()
        assert steps[0]["status"] == "error"
        assert steps[0]["error"] == "command failed"

    def test_record_end_without_start_skipped(self) -> None:
        # 无 session 存在，record_end 直接返回
        tracker = ToolUseTracker()
        tracker.record_end("orphan", output="late result")
        assert tracker.build_display_steps() == []

    def test_multiple_steps(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "a.py")
        tracker.record_start("exec", "cmd")
        tracker.record_end("read", output="content")
        tracker.record_end("exec", error="fail")
        steps = tracker.build_display_steps()
        assert len(steps) == 2
        assert steps[0]["status"] == "success"
        assert steps[1]["status"] == "error"

    def test_max_steps_limit(self) -> None:
        tracker = ToolUseTracker(max_steps=2)
        tracker.record_start("a", "")
        tracker.record_start("b", "")
        tracker.record_start("c", "")  # 应被丢弃
        steps = tracker.build_display_steps()
        assert len(steps) == 2

    def test_display_steps_have_icon(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "file.py")
        steps = tracker.build_display_steps()
        assert steps[0]["icon"] == "file-link-text_outlined"

    def test_unknown_tool_gets_default_icon(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("custom_tool_xyz", "")
        steps = tracker.build_display_steps()
        assert steps[0]["icon"] == "setting-inter_outlined"

    def test_no_result_tools_skip_result_block(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "file.py")
        tracker.record_end("read", output="file contents")
        steps = tracker.build_display_steps()
        assert steps[0]["result_block"] is None

    def test_result_tools_have_result_block(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("exec", "cmd")
        tracker.record_end("exec", output="output data")
        steps = tracker.build_display_steps()
        assert steps[0]["result_block"] is not None

    def test_elapsed_ms_positive_after_start(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("read", "f")
        time.sleep(0.001)
        assert tracker.elapsed_ms > 0.0

    def test_detail_sanitized(self) -> None:
        tracker = ToolUseTracker()
        tracker.record_start("exec", "token=secret_value")
        steps = tracker.build_display_steps()
        assert "secret_value" not in steps[0]["detail"]

    @patch("hermes_lark_streaming.tooluse.time")
    def test_step_elapsed_ms_recorded(self, mock_time: object) -> None:
        mock_time.time.side_effect = [100.0, 100.0, 102.5]
        tracker = ToolUseTracker()
        tracker.record_start("read", "f")
        tracker.record_end("read", output="ok")
        steps = tracker.build_display_steps()
        assert steps[0]["elapsed_ms"] == pytest.approx(2500.0)
