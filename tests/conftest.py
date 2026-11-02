"""Hermes 集成测试使用独立临时配置，不读取用户的运行配置."""

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    from hermes_lark_streaming.streaming import unavailable_guard

    monkeypatch.setattr(unavailable_guard, "_unavailable_cache", {})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "test-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "test-secret")
