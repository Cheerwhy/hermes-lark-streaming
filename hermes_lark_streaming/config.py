"""读取 Hermes 配置，提供本插件所需的配置项."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_HERMES_CONFIG_PATH = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"

DEFAULT_DOMAIN = "https://open.feishu.cn"  # SDK 根域名，Larksuite 用 https://open.larksuite.com


class Config:
    """插件配置，惰性读取 Hermes 主配置."""

    def __init__(self) -> None:
        self._raw: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        """是否启用流式卡片."""
        sec = self._streaming_sec()
        return bool(sec.get("enabled", False))

    @property
    def panel_expanded(self) -> bool:
        """完成态卡片中面板（工具、推理）是否保持展开."""
        sec = self._streaming_sec()
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
    def feishu_app_id(self) -> str:
        return str(self._platform_cfg().get("app_id", ""))

    @property
    def feishu_app_secret(self) -> str:
        return str(self._platform_cfg().get("app_secret", ""))

    @property
    def feishu_base_url(self) -> str:
        return str(self._platform_cfg().get("base_url", DEFAULT_DOMAIN))

    @property
    def card_duration_sec(self) -> int:
        """卡片存活检测超时."""
        return int(self._streaming_sec().get("card_ttl_sec", 600))

    @property
    def header_enabled(self) -> bool:
        """流式卡片和完成态卡片是否显示 header."""
        sec = self._streaming_sec()
        header = sec.get("header", {})
        if not isinstance(header, dict):
            return False
        return bool(header.get("enabled", False))

    @property
    def footer_enabled(self) -> bool:
        """完成态卡片是否显示 footer."""
        sec = self._streaming_sec()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return True
        return bool(footer.get("enabled", True))

    @property
    def body_text_size(self) -> str:
        """Body answer markdown 的文字大小."""
        sec = self._streaming_sec()
        body = sec.get("body", {})
        if not isinstance(body, dict):
            return "normal_v2"
        return str(body.get("text_size", "normal_v2")) or "normal_v2"

    @property
    def footer_text_size(self) -> str:
        """Footer markdown 的文字大小."""
        sec = self._streaming_sec()
        footer = sec.get("footer", {})
        if not isinstance(footer, dict):
            return "notation"
        return str(footer.get("text_size", "notation")) or "notation"

    @property
    def footer_fields(self) -> list[list[str]]:
        """Footer 字段布局（二维数组）."""
        sec = self._streaming_sec()
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
        sec = self._streaming_sec()
        footer = sec.get("footer", {})
        return bool(footer.get("show_label", False))

    @staticmethod
    def _default_footer_fields() -> list[list[str]]:
        return [["status", "elapsed", "context", "model"]]

    @property
    def env_app_id(self) -> str:
        return os.environ.get("FEISHU_APP_ID") or os.environ.get("LARK_APP_ID") or ""

    @property
    def env_app_secret(self) -> str:
        return os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LARK_APP_SECRET") or ""

    def _streaming_sec(self) -> dict[str, Any]:
        raw = self._load()
        sec = raw.get("streaming") or {}
        if isinstance(sec, dict):
            return sec
        return {}

    def _platform_cfg(self) -> dict[str, Any]:
        """从环境变量或平台配置找飞书凭据."""
        if self.env_app_id and self.env_app_secret:
            return {
                "app_id": self.env_app_id,
                "app_secret": self.env_app_secret,
                "base_url": os.environ.get(
                    "FEISHU_BASE_URL",
                    os.environ.get("LARK_BASE_URL", DEFAULT_DOMAIN),
                ),
            }
        raw = self._load()
        for key in ("feishu", "lark"):
            pf = raw.get(key)
            if isinstance(pf, dict) and pf.get("app_id"):
                return pf
        return {}

    def _load(self) -> dict[str, Any]:
        if self._raw is not None:
            return self._raw
        if _HERMES_CONFIG_PATH.exists():
            text = _HERMES_CONFIG_PATH.read_text(encoding="utf-8")
            self._raw = yaml.safe_load(text) or {}
        else:
            self._raw = {}
        return self._raw

    def _reload(self) -> dict[str, Any]:
        """从磁盘重新读取配置（不更新缓存），供运行时可变的配置项使用."""
        if _HERMES_CONFIG_PATH.exists():
            text = _HERMES_CONFIG_PATH.read_text(encoding="utf-8")
            return yaml.safe_load(text) or {}
        return {}
