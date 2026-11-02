"""Hermes >= 0.22.0 的正式平台注册入口."""

from __future__ import annotations

import re
from typing import Any

PLATFORM = "feishu"


def check_host() -> None:
    from hermes_cli import plugins
    from hermes_cli.version_info import get_version_info

    from .__main__ import legacy_files

    legacy = legacy_files()
    if legacy:
        raise RuntimeError(
            f"Old injections detected in {', '.join(legacy)}; uninstall them with the OLD plugin release first"
        )
    version = get_version_info().base_version
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if match and tuple(map(int, match.groups())) < (0, 22, 0):
        raise RuntimeError("hermes-lark-streaming requires Hermes >= 0.22.0")
    required = {
        "on_stream_start", "on_stream_delta", "pre_tool_call", "post_tool_call", "post_api_request", "on_session_end",
    }
    if not required <= plugins.VALID_HOOKS or not hasattr(plugins.PluginContext, "register_platform"):
        raise RuntimeError("Hermes native platform/stream hooks unavailable; Hermes >= 0.22.0 required")


def register(ctx: Any) -> None:
    check_host()
    from plugins.platforms.feishu import adapter as builtin

    from .adapter import FeishuStreamingAdapter
    from .hooks import (
        observe_end,
        observe_reasoning,
        observe_start,
        observe_tool_end,
        observe_tool_start,
        observe_usage,
    )
    from .sender import standalone_send

    def configured(config: Any) -> bool:
        settings = builtin.FeishuAdapter._load_settings(config.extra or {})
        return bool(settings.app_id and settings.app_secret)

    ctx.register_platform(
        name=PLATFORM,
        label="Feishu / Lark Streaming Cards",
        adapter_factory=FeishuStreamingAdapter,
        check_fn=builtin.feishu_deps_present,
        ensure_deps_fn=builtin.check_feishu_requirements,
        validate_config=configured,
        is_connected=configured,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        allowed_users_env="FEISHU_ALLOWED_USERS",
        allow_all_env="FEISHU_ALLOW_ALL_USERS",
        setup_fn=builtin.interactive_setup,
        apply_yaml_config_fn=builtin._apply_yaml_config,
        cron_deliver_env_var="FEISHU_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        max_message_length=8000,
        emoji="🪽",
    )
    for name, callback in (
        ("on_stream_start", observe_start),
        ("on_stream_delta", observe_reasoning),
        ("pre_tool_call", observe_tool_start),
        ("post_tool_call", observe_tool_end),
        ("post_api_request", observe_usage),
        ("on_session_end", observe_end),
    ):
        ctx.register_hook(name, callback)
