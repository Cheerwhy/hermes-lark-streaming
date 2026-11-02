"""Hermes 原生飞书流式卡片平台插件."""

from typing import Any

__version__ = "0.14.0"


def register(ctx: Any) -> None:
    """延迟导入宿主，渲染库可在 Hermes 之外单独使用."""
    from .plugin import register as register_plugin

    register_plugin(ctx)
