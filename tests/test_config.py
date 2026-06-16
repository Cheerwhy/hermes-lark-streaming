"""config.py 测试 — 配置加载、footer 字段容错、平台配置优先级."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch

from hermes_lark_streaming.config import Config


def _make_config(raw: dict[str, Any]) -> Config:
    """Create a Config pre-loaded with given raw dict."""
    cfg = Config()
    cfg._raw = raw
    return cfg


class TestEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"streaming": {"enabled": True}})
        assert cfg.enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"streaming": {"enabled": False}})
        assert cfg.enabled is False

    def test_enabled_missing(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.enabled is False

    def test_no_streaming_section(self) -> None:
        cfg = _make_config({})
        assert cfg.enabled is False

    def test_streaming_section_not_dict(self) -> None:
        cfg = _make_config({"streaming": "invalid"})
        assert cfg.enabled is False


class TestFooterFields:
    def test_normal_2d_fields(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": [["a", "b"], ["c"]]}}})
        assert cfg.footer_fields == [["a", "b"], ["c"]]

    def test_1d_auto_wrapped(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": ["status", "elapsed"]}}})
        assert cfg.footer_fields == [["status", "elapsed"]]

    def test_empty_fields_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": []}}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_no_footer_returns_default(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_footer_not_dict_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": "invalid"}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_fields_non_list_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": "status"}}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]


class TestHeaderEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"streaming": {"header": {"enabled": True}}})
        assert cfg.header_enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"streaming": {"header": {"enabled": False}}})
        assert cfg.header_enabled is False

    def test_missing_enabled_key_defaults_false(self) -> None:
        cfg = _make_config({"streaming": {"header": {}}})
        assert cfg.header_enabled is False

    def test_missing_header_section_defaults_false(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.header_enabled is False

    def test_header_not_dict_defaults_false(self) -> None:
        cfg = _make_config({"streaming": {"header": "invalid"}})
        assert cfg.header_enabled is False


class TestFooterEnabled:
    def test_enabled_true(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"enabled": True}}})
        assert cfg.footer_enabled is True

    def test_enabled_false(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"enabled": False}}})
        assert cfg.footer_enabled is False

    def test_missing_enabled_key_defaults_true(self) -> None:
        cfg = _make_config({"streaming": {"footer": {}}})
        assert cfg.footer_enabled is True

    def test_no_footer_section_defaults_true(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.footer_enabled is True

    def test_footer_not_dict_defaults_true(self) -> None:
        cfg = _make_config({"streaming": {"footer": "invalid"}})
        assert cfg.footer_enabled is True


class TestFooterShowLabel:
    def test_true(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"show_label": True}}})
        assert cfg.footer_show_label is True

    def test_false(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"show_label": False}}})
        assert cfg.footer_show_label is False

    def test_missing_defaults_false(self) -> None:
        cfg = _make_config({"streaming": {"footer": {}}})
        assert cfg.footer_show_label is False


class TestCardDurationSec:
    def test_custom(self) -> None:
        cfg = _make_config({"streaming": {"card_ttl_sec": 300}})
        assert cfg.card_duration_sec == 300

    def test_default(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.card_duration_sec == 600


class TestFeishuAppId:
    def test_from_env(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            assert cfg.feishu_app_id == "env_id"

    def test_from_config(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "cfg_id", "app_secret": "cfg_secret"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_app_id == "cfg_id"

    def test_empty_when_missing(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_app_id == ""


class TestFeishuBaseURL:
    def test_default_url(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "id", "app_secret": "s"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_base_url == "https://open.feishu.cn/open-apis"

    def test_custom_url_from_config(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "id", "app_secret": "s", "base_url": "https://custom.com"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_base_url == "https://custom.com"

    def test_from_env(self) -> None:
        cfg = _make_config({})
        with patch.dict(
            os.environ, {"FEISHU_APP_ID": "id", "FEISHU_APP_SECRET": "s", "FEISHU_BASE_URL": "https://env.com"}
        ):
            assert cfg.feishu_base_url == "https://env.com"


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
        cfg = self._make_reasoning_config({"streaming": {"enabled": True}})
        assert cfg.show_reasoning is False


class TestPlatformCfg:
    def test_env_takes_priority(self) -> None:
        cfg = _make_config({"feishu": {"app_id": "config_id", "app_secret": "config_secret"}})
        with patch.dict(os.environ, {"FEISHU_APP_ID": "env_id", "FEISHU_APP_SECRET": "env_secret"}):
            result = cfg._platform_cfg()
            assert result["app_id"] == "env_id"

    def test_lark_section_fallback(self) -> None:
        cfg = _make_config({"lark": {"app_id": "lark_id", "app_secret": "lark_secret"}})
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "lark_id"

    def test_feishu_before_lark(self) -> None:
        cfg = _make_config(
            {
                "feishu": {"app_id": "feishu_id", "app_secret": "fs"},
                "lark": {"app_id": "lark_id", "app_secret": "ls"},
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "feishu_id"

    def test_empty_when_nothing(self) -> None:
        cfg = _make_config({})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}
