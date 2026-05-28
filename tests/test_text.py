"""streaming.text 测试 — reasoning 标签解析."""

from __future__ import annotations

from hermes_lark_streaming.streaming.text import (
    extract_thinking_content,
    split_reasoning_text,
    strip_reasoning_tags,
)


class TestSplitReasoningText:
    def test_none_returns_empty(self) -> None:
        assert split_reasoning_text(None) == {}

    def test_empty_string_returns_empty(self) -> None:
        assert split_reasoning_text("") == {}

    def test_whitespace_only_returns_empty(self) -> None:
        assert split_reasoning_text("   \n  ") == {}

    def test_plain_text_no_tags(self) -> None:
        assert split_reasoning_text("Hello world") == {"answer_text": "Hello world"}

    def test_reasoning_prefix(self) -> None:
        result = split_reasoning_text("Reasoning:\nstep 1\nstep 2")
        assert result.keys() == {"reasoning_text"}
        assert "step 1" in result["reasoning_text"]

    def test_reasoning_prefix_strips_underscore_lines(self) -> None:
        result = split_reasoning_text("Reasoning:\n_thinking_\ndone")
        assert "_thinking_" not in (result.get("reasoning_text") or "")

    def test_reasoning_prefix_too_short_ignored(self) -> None:
        # "Reasoning:\n" 单独存在不比前缀长，应走普通文本逻辑
        assert split_reasoning_text("Reasoning:\n") == {"answer_text": "Reasoning:\n"}

    def test_thinking_tags(self) -> None:
        text = "<thinking>deep thoughts</thinking>answer here"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "deep thoughts"
        # strip_reasoning_tags 移除标签但保留标签间内容
        assert "answer here" in result["answer_text"]

    def test_thought_tags(self) -> None:
        text = "<thought>reasoning</thought>the answer"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "reasoning"
        assert "the answer" in result["answer_text"]

    def test_antthinking_tags(self) -> None:
        text = "<antthinking>model thoughts</antthinking>response"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "model thoughts"
        assert "response" in result["answer_text"]

    def test_tags_with_whitespace(self) -> None:
        text = "< thinking >content< /thinking >rest"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "content"

    def test_unclosed_tag(self) -> None:
        text = "<thinking>ongoing reasoning"
        result = split_reasoning_text(text)
        assert result["reasoning_text"] == "ongoing reasoning"
        # reasoning_text 和 answer_text 都包含内容
        assert result["answer_text"] is not None


class TestExtractThinkingContent:
    def test_empty_string(self) -> None:
        assert extract_thinking_content("") == ""

    def test_no_tags(self) -> None:
        assert extract_thinking_content("plain text") == ""

    def test_single_pair(self) -> None:
        assert extract_thinking_content("<thinking>hello</thinking>") == "hello"

    def test_multiple_pairs(self) -> None:
        text = "<thinking>part1</thinking>ignored<thinking>part2</thinking>"
        assert extract_thinking_content(text) == "part1part2"

    def test_unclosed_tag_extracts_till_end(self) -> None:
        assert extract_thinking_content("<thinking>rest of text") == "rest of text"

    def test_case_insensitive(self) -> None:
        assert extract_thinking_content("<THOUGHT>content</THOUGHT>") == "content"


class TestStripReasoningTags:
    def test_removes_tag_markers(self) -> None:
        # 标签被移除，但标签间内容保留
        result = strip_reasoning_tags("<thinking>content</thinking>")
        assert "<thinking>" not in result
        assert "</thinking>" not in result

    def test_mixed_text_keeps_surrounding(self) -> None:
        text = "before<thinking>inner</thinking>after"
        result = strip_reasoning_tags(text)
        assert "before" in result
        assert "after" in result
        # 标签标记被移除
        assert "<thinking>" not in result

    def test_no_tags_unchanged(self) -> None:
        assert strip_reasoning_tags("no tags here") == "no tags here"

    def test_reasoning_prefix_clears_all(self) -> None:
        result = strip_reasoning_tags("Reasoning:\nsome content")
        assert result.strip() == ""
