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

    def test_no_fields_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": {}}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_no_footer_returns_default(self) -> None:
        cfg = _make_config({"streaming": {}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_footer_not_dict_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": "invalid"}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_no_streaming_section_returns_default(self) -> None:
        cfg = _make_config({})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_fields_non_list_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": "status"}}})
        assert cfg.footer_fields == [["status", "elapsed", "context", "model"]]

    def test_fields_int_returns_default(self) -> None:
        cfg = _make_config({"streaming": {"footer": {"fields": 42}}})
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

    def test_no_streaming_section_defaults_false(self) -> None:
        cfg = _make_config({})
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

    def test_no_streaming_section_defaults_true(self) -> None:
        cfg = _make_config({})
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


class TestPlatformsExtraLayout:
    """Test the canonical Hermes multi-profile layout:
    ``platforms.feishu.extra.app_id`` / ``platforms.feishu.extra.app_secret``.

    Each Hermes profile (claudia, bill, cody, hazel, laura, onix, pos) binds a
    different Feishu bot, with credentials stored under platforms.<name>.extra
    in its own config.yaml. The plugin must read from this layout so streaming
    cards work per-profile instead of being silently disabled.
    """

    def test_platforms_feishu_extra(self) -> None:
        cfg = _make_config(
            {
                "platforms": {
                    "feishu": {
                        "extra": {
                            "app_id": "pf_id",
                            "app_secret": "pf_secret",
                            "domain": "feishu",
                        }
                    }
                }
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "pf_id"
            assert result["app_secret"] == "pf_secret"

    def test_platforms_lark_extra(self) -> None:
        cfg = _make_config(
            {
                "platforms": {
                    "lark": {
                        "extra": {
                            "app_id": "lk_id",
                            "app_secret": "lk_secret",
                        }
                    }
                }
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "lk_id"
            assert result["app_secret"] == "lk_secret"

    def test_platforms_extra_with_base_url(self) -> None:
        cfg = _make_config(
            {
                "platforms": {
                    "feishu": {
                        "base_url": "https://custom.example.com",
                        "extra": {"app_id": "u_id", "app_secret": "u_secret"},
                    }
                }
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_base_url == "https://custom.example.com"
            assert cfg.feishu_app_id == "u_id"

    def test_platforms_extra_missing_app_secret(self) -> None:
        cfg = _make_config(
            {
                "platforms": {
                    "feishu": {
                        "extra": {
                            "app_id": "only_id",
                        }
                    }
                }
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.feishu_app_id == "only_id"
            assert cfg.feishu_app_secret == ""

    def test_platforms_feishu_without_extra_falls_through(self) -> None:
        """``platforms.feishu`` may exist (with display flags) but no ``extra`` —
        in that case resolution must continue to the next strategy rather than
        returning an empty dict."""
        cfg = _make_config(
            {
                "platforms": {
                    "feishu": {
                        "streaming": True,
                        "busy_ack_detail": False,
                    }
                }
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}

    def test_platforms_not_dict(self) -> None:
        cfg = _make_config({"platforms": "not_a_dict"})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}

    def test_platforms_feishu_not_dict(self) -> None:
        cfg = _make_config({"platforms": {"feishu": "not_a_dict"}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}

    def test_extra_not_dict(self) -> None:
        cfg = _make_config({"platforms": {"feishu": {"extra": "not_a_dict"}}})
        with patch.dict(os.environ, {}, clear=True):
            assert cfg._platform_cfg() == {}

    def test_top_level_feishu_still_wins_over_platforms_extra(self) -> None:
        """Backwards compat: top-level ``feishu:`` is still preferred over
        ``platforms.feishu.extra`` when both are present (preserves the existing
        test_feishu_before_lark / test_lark_section_fallback precedence)."""
        cfg = _make_config(
            {
                "feishu": {"app_id": "top_id", "app_secret": "top_secret"},
                "platforms": {
                    "feishu": {"extra": {"app_id": "pf_id", "app_secret": "pf_secret"}}
                },
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            result = cfg._platform_cfg()
            assert result["app_id"] == "top_id"
            assert result["app_secret"] == "top_secret"

    def test_realistic_claudia_profile_layout(self) -> None:
        """End-to-end: a realistic per-profile config with the same shape
        claudia/bill/cody/etc. actually use must yield the expected credentials."""
        cfg = _make_config(
            {
                "model": {"provider": "minimax-cn"},
                "streaming": {"enabled": True},
                "display": {
                    "platforms": {
                        "feishu": {
                            "busy_ack_detail": False,
                            "tool_progress": "off",
                        }
                    }
                },
                "platforms": {
                    "feishu": {
                        "extra": {
                            "app_id": "cli_real",
                            "app_secret": "real_secret",
                            "connection_mode": "websocket",
                            "default_group_policy": "open",
                            "domain": "feishu",
                        }
                    },
                    "telegram": {
                        "extra": {"bot_token": "123:abc"},
                    },
                },
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            assert cfg.enabled is True
            assert cfg.feishu_app_id == "cli_real"
            assert cfg.feishu_app_secret == "real_secret"


class TestEnvFileLoading:
    """Test that ``$HERMES_HOME/.env`` is sourced at Config construction, so
    CLI invocations (status, install, verify) see credentials the same way
    the gateway process does. Existing env vars win — callers can override."""

    def test_env_file_loaded_into_environ(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath("FEISHU_APP_ID=dotenv_id\nFEISHU_APP_SECRET=dotenv_secret\n"),
        ):
            cfg = Config()
            assert cfg.env_app_id == "dotenv_id"
            assert cfg.env_app_secret == "dotenv_secret"

    def test_existing_env_wins_over_env_file(self) -> None:
        with patch.dict(
            os.environ,
            {"FEISHU_APP_ID": "explicit_id", "FEISHU_APP_SECRET": "explicit_secret"},
            clear=True,
        ), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath("FEISHU_APP_ID=dotenv_id\nFEISHU_APP_SECRET=dotenv_secret\n"),
        ):
            cfg = Config()
            assert cfg.env_app_id == "explicit_id"
            assert cfg.env_app_secret == "explicit_secret"

    def test_env_file_quoted_values_stripped(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath('FEISHU_APP_ID="qd_id"\nFEISHU_APP_SECRET=\'qs_secret\'\n'),
        ):
            cfg = Config()
            assert cfg.env_app_id == "qd_id"
            assert cfg.env_app_secret == "qs_secret"

    def test_env_file_comments_and_blank_lines_skipped(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath("# top comment\n\nFEISHU_APP_ID=skip_id\nFEISHU_APP_SECRET=skip_secret\n"),
        ):
            cfg = Config()
            assert cfg.env_app_id == "skip_id"
            assert cfg.env_app_secret == "skip_secret"

    def test_env_file_malformed_lines_silently_skipped(self) -> None:
        """Lines without ``=`` are ignored — they shouldn't crash the loader."""
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath("garbage_no_equals\nFEISHU_APP_ID=ok_id\nFEISHU_APP_SECRET=ok_secret\n"),
        ):
            cfg = Config()
            assert cfg.env_app_id == "ok_id"
            assert cfg.env_app_secret == "ok_secret"

    def test_missing_env_file_is_noop(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "hermes_lark_streaming.config._HERMES_ENV_PATH",
            _FakePath(""),  # simulates nonexistent file
        ):
            cfg = Config()
            assert cfg.env_app_id == ""
            assert cfg.env_app_secret == ""


class _FakePath:
    """A minimal stand-in for a pathlib.Path whose .is_file() / .read_text()
    match the bytes we hand it. Lets us patch ``_HERMES_ENV_PATH`` without
    touching the real filesystem."""

    def __init__(self, content: str) -> None:
        self._content = content
        self._exists = bool(content)

    def is_file(self) -> bool:
        return self._exists

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._content

