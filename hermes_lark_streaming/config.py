"""读取 Hermes 配置，提供本插件所需的配置项."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_HERMES_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"


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
    def linear(self) -> bool:
        """线性模式：工具调用和回复顺序展示."""
        sec = self._streaming_sec()
        return bool(sec.get("linear", False))

    @property
    def feishu_app_id(self) -> str:
        return str(self._platform_cfg().get("app_id", ""))

    @property
    def feishu_app_secret(self) -> str:
        return str(self._platform_cfg().get("app_secret", ""))

    @property
    def feishu_base_url(self) -> str:
        return str(self._platform_cfg().get("base_url", "https://open.feishu.cn/open-apis"))

    @property
    def card_duration_sec(self) -> int:
        """卡片存活检测超时."""
        return int(self._streaming_sec().get("card_ttl_sec", 600))

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
                    os.environ.get("LARK_BASE_URL", "https://open.feishu.cn/open-apis"),
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
