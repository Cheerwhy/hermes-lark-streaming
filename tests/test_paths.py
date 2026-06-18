"""Hermes 路径发现测试 — 单一来源、HERMES_HOME 响应、多候选代码根、importlib 兜底."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hermes_lark_streaming import config as config_mod
from hermes_lark_streaming import patcher as patcher_mod
from hermes_lark_streaming.config import hermes_home
from hermes_lark_streaming.patcher import (
    CronPatcher,
    Patcher,
    PatcherError,
    _code_roots,
    _default_cron_path,
    _default_run_path,
    _resolve_module_path,
)


def test_single_source() -> None:
    """hermes_home() 是 Hermes 主目录的唯一定义方，patcher 不应重复定义。"""
    assert not hasattr(patcher_mod, "_HERMES_HOME")
    assert patcher_mod.hermes_home is config_mod.hermes_home


def test_hermes_home_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert hermes_home() == Path.home() / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    assert hermes_home() == tmp_path / "a"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    assert hermes_home() == tmp_path / "b"


def test_code_roots_includes_root_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_code_roots() 含 root-mode 固定路径 /usr/local/lib/hermes-agent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    roots = _code_roots()
    assert tmp_path / "hermes-agent" in roots
    assert Path("/usr/local/lib/hermes-agent") in roots


@pytest.mark.parametrize(
    ("module_name", "rel"),
    [("gateway.run", "gateway/run.py"), ("cron.scheduler", "cron/scheduler.py")],
)
def test_resolve_first_root_hit(module_name: str, rel: str, tmp_path: Path) -> None:
    """第一个候选根命中标准布局。"""
    target = tmp_path / "hermes-agent" / rel
    target.parent.mkdir(parents=True)
    target.write_text("# stub\n")
    assert _resolve_module_path(module_name, [tmp_path / "hermes-agent"]) == target.resolve()


def test_resolve_falls_through_to_second_root(tmp_path: Path) -> None:
    """第一个候选不存在时，落到第二个候选命中。"""
    second = tmp_path / "lib" / "hermes-agent"
    target = second / "gateway" / "run.py"
    target.parent.mkdir(parents=True)
    target.write_text("# stub\n")
    # 第一个候选 tmp_path/hermes-agent 不存在，应继续到 second
    assert _resolve_module_path("gateway.run", [tmp_path / "hermes-agent", second]) == target.resolve()


def test_resolve_falls_back_to_first_root_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """所有候选和 importlib 都找不到时，返回第一个候选下的路径（即使不存在）。

    屏蔽 find_spec，避免命中测试环境真实安装的 gateway/cron 包。
    """
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    root = tmp_path / "hermes-agent"
    assert _resolve_module_path("gateway.run", [root]) == (root / "gateway" / "run.py")


@pytest.mark.parametrize(
    ("default_path", "rel"),
    [(_default_run_path, "gateway/run.py"), (_default_cron_path, "cron/scheduler.py")],
)
def test_default_path_respects_hermes_home(
    default_path: object, rel: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认路径用当前 HERMES_HOME 计算，不冻结于 import 时。"""
    target = tmp_path / "hermes-agent" / rel
    target.parent.mkdir(parents=True)
    target.write_text("# stub\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert default_path() == target.resolve()  # type: ignore[operator]


@pytest.mark.parametrize(
    ("cls", "label"),
    [(Patcher, "gateway/run.py"), (CronPatcher, "scheduler.py")],
)
def test_not_found_diagnostic_lists_tried_roots(
    cls: type, label: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """找不到目标时报错列出所有尝试过的候选根 + HERMES_HOME 提示。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(PatcherError) as exc_info:
        cls()
    msg = str(exc_info.value)
    assert label in msg
    assert "tried:" in msg
    assert str(tmp_path / "hermes-agent") in msg
    assert "/usr/local/lib/hermes-agent" in msg
    assert "HERMES_HOME" in msg


def test_explicit_path_bypasses_discovery(tmp_path: Path) -> None:
    """显式传 path 时不走发现逻辑，直接用传入值。"""
    run_py = tmp_path / "run.py"
    run_py.write_text("# stub\n")
    assert Patcher(run_path=run_py).run_path == run_py
