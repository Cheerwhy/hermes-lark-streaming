"""config.py 测试 — 配置加载、footer 字段容错、平台配置优先级."""

from __future__ import annotations

from typing import Any

import pytest

from hermes_lark_streaming.config import Config


def _make_config(raw: dict[str, Any]) -> Config:
    """绑定原生平台 extra.card 配置."""
    return Config(extra={"card": raw.get("card", {})})


class TestEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"card": {"enabled": True}})
        assert cfg.enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"card": {"enabled": False}})
        assert cfg.enabled is False

    @pytest.mark.parametrize("raw", [{"card": {}}, {}], ids=["missing-key", "missing-section"])
    def test_enabled_defaults_true_when_missing(self, raw: dict[str, Any]) -> None:
        cfg = _make_config(raw)
        assert cfg.enabled is True

    def test_card_section_not_dict(self) -> None:
        cfg = _make_config({"card": "invalid"})
        assert cfg.enabled is True


class TestFooterFields:
    def test_normal_2d_fields(self) -> None:
        cfg = _make_config({"card": {"footer": {"fields": [["a", "b"], ["c"]]}}})
        assert cfg.footer_fields == [["a", "b"], ["c"]]

    def test_1d_auto_wrapped(self) -> None:
        cfg = _make_config({"card": {"footer": {"fields": ["status", "elapsed"]}}})
        assert cfg.footer_fields == [["status", "elapsed"]]

    @pytest.mark.parametrize(
        "raw",
        [{"card": {"footer": {"fields": []}}}, {"card": {}}],
        ids=["empty-fields", "missing-footer"],
    )
    def test_empty_footer_configuration_returns_default(self, raw: dict[str, Any]) -> None:
        cfg = _make_config(raw)
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_footer_not_dict_returns_default(self) -> None:
        cfg = _make_config({"card": {"footer": "invalid"}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_fields_non_list_returns_default(self) -> None:
        cfg = _make_config({"card": {"footer": {"fields": "status"}}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]


class TestHeaderEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"card": {"header": {"enabled": True}}})
        assert cfg.header_enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"card": {"header": {"enabled": False}}})
        assert cfg.header_enabled is False

    @pytest.mark.parametrize(
        "raw",
        [{"card": {"header": {}}}, {"card": {}}],
        ids=["missing-key", "missing-section"],
    )
    def test_header_enabled_defaults_false_when_missing(self, raw: dict[str, Any]) -> None:
        cfg = _make_config(raw)
        assert cfg.header_enabled is False

    def test_header_not_dict_defaults_false(self) -> None:
        cfg = _make_config({"card": {"header": "invalid"}})
        assert cfg.header_enabled is False


class TestFooterEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"card": {"footer": {"enabled": True}}})
        assert cfg.footer_enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"card": {"footer": {"enabled": False}}})
        assert cfg.footer_enabled is False

    @pytest.mark.parametrize(
        "raw",
        [{"card": {"footer": {}}}, {"card": {}}],
        ids=["missing-key", "missing-section"],
    )
    def test_footer_enabled_defaults_true_when_missing(self, raw: dict[str, Any]) -> None:
        cfg = _make_config(raw)
        assert cfg.footer_enabled is True

    def test_footer_not_dict_defaults_true(self) -> None:
        cfg = _make_config({"card": {"footer": "invalid"}})
        assert cfg.footer_enabled is True


class TestFooterShowLabel:
    @pytest.mark.parametrize("value", [True, False])
    def test_reads_boolean_value(self, value: bool) -> None:
        cfg = _make_config({"card": {"footer": {"show_label": value}}})
        assert cfg.footer_show_label is value

    def test_missing_defaults_false(self) -> None:
        cfg = _make_config({"card": {"footer": {}}})
        assert cfg.footer_show_label is False


class TestWidthMode:
    def test_default_when_missing(self) -> None:
        cfg = _make_config({"card": {}})
        assert cfg.width_mode == "default"

    def test_reads_valid_value(self) -> None:
        cfg = _make_config({"card": {"width_mode": "compact"}})
        assert cfg.width_mode == "compact"

    def test_reads_case_insensitive(self) -> None:
        cfg = _make_config({"card": {"width_mode": "FILL"}})
        assert cfg.width_mode == "fill"

    def test_invalid_falls_back_to_default(self) -> None:
        cfg = _make_config({"card": {"width_mode": "wide"}})
        assert cfg.width_mode == "default"


class TestShowReasoning:
    def _make_reasoning_config(self, raw: dict[str, Any]) -> Config:
        """Create a Config with _reload mocked to return given raw dict."""
        cfg = Config()
        cfg._reload = lambda: raw  # type: ignore[assignment]
        return cfg

    def test_platform_level_true(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"show_reasoning": True}}}})
        assert cfg.show_reasoning is True

    def test_platform_level_false(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"show_reasoning": False}}}})
        assert cfg.show_reasoning is False

    def test_global_fallback_true(self) -> None:
        cfg = self._make_reasoning_config({"display": {"show_reasoning": True}})
        assert cfg.show_reasoning is True

    def test_global_fallback_false(self) -> None:
        cfg = self._make_reasoning_config({"display": {"show_reasoning": False}})
        assert cfg.show_reasoning is False

    def test_default_false(self) -> None:
        cfg = self._make_reasoning_config({})
        assert cfg.show_reasoning is False

    def test_display_not_dict(self) -> None:
        cfg = self._make_reasoning_config({"display": "invalid"})
        assert cfg.show_reasoning is False

    def test_platforms_not_dict(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": "invalid"}})
        assert cfg.show_reasoning is False

    def test_feishu_section_missing_key(self) -> None:
        cfg = self._make_reasoning_config({"display": {"platforms": {"feishu": {"other": True}}}})
        assert cfg.show_reasoning is False

    def test_platform_takes_priority_over_global(self) -> None:
        cfg = self._make_reasoning_config({
            "display": {
                "platforms": {"feishu": {"show_reasoning": False}},
                "show_reasoning": True,
            }
        })
        assert cfg.show_reasoning is False

    def test_no_display_section(self) -> None:
        cfg = self._make_reasoning_config({"card": {"enabled": True}})
        assert cfg.show_reasoning is False


class TestShowToolUse:
    def _make_config(self, raw: dict[str, Any]) -> Config:
        cfg = Config()
        cfg._reload = lambda: raw  # type: ignore[assignment]
        return cfg

    def test_platform_level_true(self) -> None:
        cfg = self._make_config({"display": {"platforms": {"feishu": {"show_tool_use": True}}}})
        assert cfg.show_tool_use is True

    def test_platform_level_false(self) -> None:
        cfg = self._make_config({"display": {"platforms": {"feishu": {"show_tool_use": False}}}})
        assert cfg.show_tool_use is False

    def test_global_fallback_true(self) -> None:
        cfg = self._make_config({"display": {"show_tool_use": True}})
        assert cfg.show_tool_use is True

    def test_global_fallback_false(self) -> None:
        cfg = self._make_config({"display": {"show_tool_use": False}})
        assert cfg.show_tool_use is False

    def test_default_true(self) -> None:
        """Missing config → default True (backward compatible)."""
        cfg = self._make_config({})
        assert cfg.show_tool_use is True

    def test_display_not_dict(self) -> None:
        cfg = self._make_config({"display": "invalid"})
        assert cfg.show_tool_use is True

    def test_platforms_not_dict(self) -> None:
        cfg = self._make_config({"display": {"platforms": "invalid"}})
        assert cfg.show_tool_use is True

    def test_feishu_section_missing_key(self) -> None:
        cfg = self._make_config({"display": {"platforms": {"feishu": {"other": True}}}})
        assert cfg.show_tool_use is True

    def test_platform_takes_priority_over_global(self) -> None:
        cfg = self._make_config({
            "display": {
                "platforms": {"feishu": {"show_tool_use": False}},
                "show_tool_use": True,
            }
        })
        assert cfg.show_tool_use is False


def test_invalid_footer_does_not_break_completion() -> None:
    assert Config(extra={"card": {"footer": "invalid"}}).footer_show_label is False


@pytest.mark.parametrize("content", ["[]", "null", "42"])
def test_non_mapping_root_uses_display_defaults(tmp_path, content) -> None:
    (tmp_path / "config.yaml").write_text(content)
    config = Config(tmp_path)
    assert config.show_tool_use is True
    assert config.show_reasoning is False
