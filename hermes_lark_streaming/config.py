"""读取 Hermes 配置，提供本插件所需的配置项."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DOMAIN = "https://open.feishu.cn"  # SDK 根域名，Larksuite 用 https://open.larksuite.com
LARK_DOMAIN = "https://open.larksuite.com"


def hermes_home() -> Path:
    """Hermes 主目录，优先级 HERMES_HOME 环境变量 → ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return Path(get_hermes_home())


def _config_path(home: Path | None = None) -> Path:
    """Hermes 主配置路径，支持绑定到指定 profile home."""
    return (home or hermes_home()) / "config.yaml"


class Config:
    """插件配置，惰性读取 Hermes 主配置."""

    def __init__(self, home: Path | None = None, *, extra: dict[str, Any] | None = None) -> None:
        self._home = (Path(home) if home is not None else hermes_home()).resolve()
        self._extra = extra or {}

    @property
    def enabled(self) -> bool:
        """是否启用流式卡片."""
        sec = self._card_options()
        return bool(sec.get("enabled", True))

    @property
    def panel_expanded(self) -> bool:
        """完成态卡片中面板（工具、推理）是否保持展开."""
        sec = self._card_options()
        return bool(sec.get("panel_expanded", False))

    @property
    def show_reasoning(self) -> bool:
        """是否展示推理过程（display.platforms.feishu.show_reasoning → display.show_reasoning）.

        每次都从磁盘重读，因为 /reasoning 命令会在运行时修改配置文件.
        """
        display = self._reload().get("display")
        if not isinstance(display, dict):
            return False
        platforms = display.get("platforms")
        if isinstance(platforms, dict):
            feishu = platforms.get("feishu")
            if isinstance(feishu, dict) and "show_reasoning" in feishu:
                return bool(feishu["show_reasoning"])
        return bool(display.get("show_reasoning", False))

    @property
    def show_tool_use(self) -> bool:
        """是否在卡片中展示工具调用面板.

        优先级：display.platforms.feishu.show_tool_use → display.show_tool_use，
        默认 True（保持向后兼容）。每次从磁盘重读以支持运行时切换。
        """
        display = self._reload().get("display")
        if not isinstance(display, dict):
            return True
        platforms = display.get("platforms")
        if isinstance(platforms, dict):
            feishu = platforms.get("feishu")
            if isinstance(feishu, dict) and "show_tool_use" in feishu:
                return bool(feishu["show_tool_use"])
        return bool(display.get("show_tool_use", True))

    @property
    def header_enabled(self) -> bool:
        """流式卡片和完成态卡片是否显示 header."""
        sec = self._card_options()
        header = sec.get("header", {})
        if not isinstance(header, dict):
            return False
        return bool(header.get("enabled", False))

    @property
    def footer_enabled(self) -> bool:
        """完成态卡片是否显示 footer."""
        sec = self._card_options()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return True
        return bool(footer.get("enabled", True))

    @property
    def body_text_size(self) -> str:
        """Body answer markdown 的文字大小."""
        sec = self._card_options()
        body = sec.get("body", {})
        if not isinstance(body, dict):
            return "normal_v2"
        return str(body.get("text_size", "normal_v2")) or "normal_v2"

    @property
    def width_mode(self) -> str:
        """Card 宽度模式: default / compact / fill."""
        raw = str(self._card_options().get("width_mode", "default") or "default").strip().lower()
        if raw in {"default", "compact", "fill"}:
            return raw
        return "default"

    @property
    def footer_text_size(self) -> str:
        """Footer markdown 的文字大小."""
        sec = self._card_options()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return "notation"
        return str(footer.get("text_size", "notation")) or "notation"

    @property
    def footer_fields(self) -> list[list[str]]:
        """Footer 字段布局（二维数组）."""
        sec = self._card_options()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return self._default_footer_fields()
        fields = footer.get("fields")
        if not fields:
            return self._default_footer_fields()
        if not isinstance(fields, list):
            return self._default_footer_fields()
        # 一维数组自动包装为二维
        if fields and isinstance(fields[0], str):
            return [fields]
        return fields

    @property
    def footer_show_label(self) -> bool:
        """Footer 是否显示字段标签."""
        sec = self._card_options()
        footer = sec.get("footer", {})
        return isinstance(footer, dict) and bool(footer.get("show_label", False))

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "context", "model"]]

    def _card_options(self) -> dict[str, Any]:
        card = self._extra.get("card", {})
        return card if isinstance(card, dict) else {}

    def _reload(self) -> dict[str, Any]:
        """从磁盘重新读取配置（不更新缓存），供运行时可变的配置项使用."""
        path = _config_path(self._home)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            config = yaml.safe_load(text)
            return config if isinstance(config, dict) else {}
        return {}
