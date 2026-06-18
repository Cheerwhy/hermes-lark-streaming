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
    _python_from_hermes_cli,
    _resolve_module_path,
    hermes_install_dir,
    hermes_python,
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


def test_hermes_python_found_via_code_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """which hermes 不可用时，_code_roots 兜底命中 <code_root>/venv/bin/python3。"""
    # 屏蔽 which hermes，强制走兜底
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: None)
    # 屏蔽系统级 root-mode 路径，避免 CI 机器命中真实安装
    monkeypatch.setattr("hermes_lark_streaming.patcher._code_roots",
                        lambda: [tmp_path / "hermes-agent"])
    py = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_python() == py


def test_hermes_python_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """which hermes 和 _code_roots 都失败时返回 None。"""
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: None)
    monkeypatch.setattr("hermes_lark_streaming.patcher._code_roots",
                        lambda: [tmp_path / "hermes-agent"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_python() is None


def test_python_from_hermes_cli_unix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unix: hermes 是 bash 脚本，解析 exec 行得到 venv/bin/python3。"""
    venv_bin = tmp_path / "hermes-agent" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").write_text("#!/bin/sh\n")
    cli_script = tmp_path / "hermes"
    cli_script.write_text(f'#!/usr/bin/env bash\nexec "{venv_bin / "hermes"}" "$@"\n')
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: str(cli_script))
    assert _python_from_hermes_cli() == venv_bin / "python3"


def test_python_from_hermes_cli_console_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pip/console_scripts 安装: hermes 是 Python 脚本，shebang 直接指向 python3。"""
    venv_bin = tmp_path / "hermes-agent" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python3"
    py.write_text("#!/bin/sh\n")
    cli_script = tmp_path / "hermes"
    # console_scripts 格式: 无 exec 行，shebang 是 python 路径
    cli_script.write_text(f"#!{py}\n# -*- coding: utf-8 -*-\nimport sys\nfrom hermes_cli.main import main\n")
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: str(cli_script))
    assert _python_from_hermes_cli() == py


def test_python_from_hermes_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes 不在 PATH 时返回 None。"""
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: None)
    assert _python_from_hermes_cli() is None


def test_hermes_install_dir_falls_back_to_code_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hermes_constants 不可用时（subprocess 失败），_code_roots 兜底命中含 gateway/run.py 的目录。"""
    # 让 hermes_python 返回一个不存在的 python（强制 subprocess 失败）
    monkeypatch.setattr("hermes_lark_streaming.patcher.shutil.which", lambda _: None)
    # 屏蔽系统级 root-mode 路径，避免 CI 机器命中真实安装
    monkeypatch.setattr("hermes_lark_streaming.patcher._code_roots",
                        lambda: [tmp_path / "hermes-agent"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # 构造 <home>/hermes-agent/gateway/run.py
    run_py = tmp_path / "hermes-agent" / "gateway" / "run.py"
    run_py.parent.mkdir(parents=True)
    run_py.write_text("# stub\n")
    assert hermes_install_dir() == (tmp_path / "hermes-agent").resolve()
