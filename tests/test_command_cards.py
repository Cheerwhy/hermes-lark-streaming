"""Tests for command card functionality."""

from hermes_lark_streaming.command_cards import (
    build_command_card,
    build_help_card,
    build_status_card,
)


def test_build_status_card():
    """Test status card builder."""
    content = "Session Status\nID: test123\nTokens: 1000"
    card = build_status_card(content)

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "blue"
    assert "📊" in card["header"]["title"]["content"]
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == content


def test_build_help_card():
    """Test help card builder."""
    content = "Available commands:\n/status - Show status\n/help - Show help"
    card = build_help_card(content)

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "green"
    assert "ℹ️" in card["header"]["title"]["content"]
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == content


def test_build_command_card():
    """Test generic command card builder."""
    # Test supported command
    card = build_command_card("status", "test content")
    assert card is not None
    assert card["schema"] == "2.0"

    # Test unsupported command
    card = build_command_card("unknown", "test content")
    assert card is None


def test_i18n_support():
    """Test that cards have i18n support."""
    card = build_status_card("test")
    title = card["header"]["title"]

    assert "i18n" in title
    assert "zh_cn" in title["i18n"]
    assert "en_us" in title["i18n"]
