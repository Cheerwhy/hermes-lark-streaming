"""Exercise complete split-source installations without touching live Hermes."""

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from hermes_sources import SPLIT_LEDGER_REVISION, SPLIT_REVISION, TARGET_REVISION, source_at

from hermes_lark_streaming import __main__ as cli
from hermes_lark_streaming import patcher as patcher_module
from hermes_lark_streaming.patcher import (
    CronPatcher,
    Patcher,
    PatcherError,
    _atomic_write,
    _clean_hooks,
    install_patchers,
)
from hermes_lark_streaming.split_gateway import GATEWAY_FILES


@pytest.fixture(params=[SPLIT_REVISION, SPLIT_LEDGER_REVISION, TARGET_REVISION],
                ids=["split", "split-ledger", "target"])
def installation(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Patcher, CronPatcher]:
    paths = ["gateway/run.py", *(f"gateway/{name}" for name in GATEWAY_FILES),
             "cron/scheduler.py", "cron/scheduler_delivery.py"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(source_at(relative, request.param), encoding="utf-8")
    return Patcher(tmp_path / "gateway/run.py"), CronPatcher(tmp_path / "cron/scheduler.py")


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_split_verify_is_read_only_and_compiles_all_targets(installation, tmp_path):
    gateway, cron = installation
    original = snapshot(tmp_path)
    assert gateway.split and cron.split
    assert cron.cron_path.name == "scheduler_delivery.py"
    for patcher in installation:
        patcher.verify_target()
        assert not patcher.is_patched()
        assert not patcher.is_fully_patched()
    assert snapshot(tmp_path) == original


def test_installed_hermes_round_trip_on_isolated_copy(tmp_path):
    root = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "hermes-agent"
    if not (root / "gateway/run_turn.py").exists():
        pytest.skip("No local split Hermes checkout; pinned-source tests cover CI")
    paths = ["gateway/run.py", *(f"gateway/{name}" for name in GATEWAY_FILES),
             "cron/scheduler.py", "cron/scheduler_delivery.py"]
    for relative in paths:
        destination = tmp_path / relative
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(root / relative, destination)
    gateway = Patcher(tmp_path / "gateway/run.py")
    cron = CronPatcher(tmp_path / "cron/scheduler.py")
    # Normalize existing installations only in the disposable copy.
    gateway.remove()
    cron.remove()
    original = snapshot(tmp_path)
    install_patchers([gateway, cron])
    assert gateway.is_fully_patched() and cron.is_fully_patched()
    gateway.remove()
    cron.remove()
    for relative, content in original.items():
        assert (tmp_path / relative).read_bytes() == content


def test_install_reinstall_uninstall_and_restore(installation, tmp_path):
    original = snapshot(tmp_path)
    install_patchers(list(installation))
    installed = snapshot(tmp_path)
    for patcher in installation:
        assert patcher.is_patched()
        assert patcher.is_fully_patched()
        patcher.verify_target()
    install_patchers(list(installation))
    assert snapshot(tmp_path) == installed
    for patcher in installation:
        patcher.remove()
        assert not patcher.is_patched()
    for relative, content in original.items():
        assert (tmp_path / relative).read_bytes() == content
    install_patchers(list(installation))
    for patcher in installation:
        patcher.restore()
        assert not patcher.is_patched()
    for relative, content in original.items():
        assert (tmp_path / relative).read_bytes() == content


def test_bad_cron_preflight_does_not_write_gateway_or_backups(installation, tmp_path):
    gateway, cron = installation
    cron.cron_path.write_text("def incompatible():\n    pass\n", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PatcherError):
        install_patchers([gateway, cron])
    assert snapshot(tmp_path) == before


def test_missing_module_fails_without_writes(installation, tmp_path):
    gateway, _cron = installation
    (gateway.run_path.parent / "run_busy.py").unlink()
    before = snapshot(tmp_path)
    with pytest.raises(PatcherError, match="Missing split gateway module"):
        gateway.apply()
    assert snapshot(tmp_path) == before


def test_partial_install_is_repaired(installation):
    gateway, cron = installation
    install_patchers([gateway, cron])
    path = gateway.run_path.parent / "run_turn_runner.py"
    path.write_text(_clean_hooks(path.read_text(), gateway.MARKERS), encoding="utf-8")
    assert gateway.is_patched()
    assert not gateway.is_fully_patched()
    install_patchers([gateway, cron])
    assert gateway.is_fully_patched()


def test_incomplete_marker_rejects_install_and_uninstall(installation, tmp_path):
    gateway, _cron = installation
    path = gateway.run_path.parent / "run_busy.py"
    path.write_text(path.read_text() + "\n# HERMES_LARK_STOP_BEGIN\n", encoding="utf-8")
    before = snapshot(tmp_path)
    for operation in (gateway.apply, gateway.remove):
        with pytest.raises(PatcherError, match="Malformed injected marker"):
            operation()
        assert snapshot(tmp_path) == before


def test_install_write_failure_rolls_back_gateway_cron_and_backups(installation, tmp_path):
    before = snapshot(tmp_path)
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path == installation[1].cron_path and not failed:
            failed = True
            raise OSError("simulated replacement failure")
        _atomic_write(path, content)

    with (
        patch.object(patcher_module, "_atomic_write", side_effect=fail_once),
        pytest.raises(OSError, match="simulated"),
    ):
        install_patchers(list(installation))
    assert failed
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fail_write", [False, True])
def test_crlf_install_restore_and_rollback_preserve_bytes(installation, tmp_path, fail_write):
    for path in tmp_path.rglob("*.py"):
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    before = snapshot(tmp_path)
    failed = False

    def write(path, content):
        nonlocal failed
        if fail_write and path == installation[1].cron_path and not failed:
            failed = True
            raise OSError("simulated CRLF write failure")
        _atomic_write(path, content)

    with patch.object(patcher_module, "_atomic_write", side_effect=write):
        if fail_write:
            with pytest.raises(OSError, match="simulated CRLF"):
                install_patchers(list(installation))
        else:
            install_patchers(list(installation))
            for patcher in installation:
                assert patcher.is_fully_patched()
                patcher.restore()
    for relative, content in before.items():
        assert (tmp_path / relative).read_bytes() == content
    if fail_write:
        assert snapshot(tmp_path) == before


@pytest.mark.parametrize("operation", ["remove", "restore"])
def test_invalid_python_inside_complete_hook_can_be_recovered(tmp_path, operation):
    path = tmp_path / "run.py"
    clean = "def _handle_message_with_agent():\n    pass\n"
    path.write_text(clean + "# HERMES_LARK_START_BEGIN\n!broken hook\n# HERMES_LARK_START_END\n")
    path.with_suffix(".py.hermes_lark.bak").write_text(clean)
    patcher = Patcher(path)
    getattr(patcher, operation)()
    assert path.read_text() == clean


def test_stale_backups_are_not_restored_and_are_refreshed_on_install(installation, tmp_path):
    install_patchers(list(installation))
    gateway, cron = installation
    path = gateway.run_path.parent / "run_turn.py"
    path.write_text(path.read_text() + "\n# Upstream changed after installation.\n", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PatcherError, match="Backup no longer matches"):
        gateway.restore()
    assert snapshot(tmp_path) == before
    # Reinstallation creates a backup of the current upstream, not an older revision.
    install_patchers([gateway, cron])
    gateway.restore()
    assert path.read_text().endswith("# Upstream changed after installation.\n")


def test_split_restore_ignores_old_monolithic_backup(installation):
    gateway, _cron = installation
    old_backup = gateway.run_path.with_suffix(".py.hermes_lark.bak")
    old_backup.write_text("# Obsolete monolithic gateway\n", encoding="utf-8")
    original = gateway.run_path.read_bytes()
    gateway.apply()
    gateway.restore()
    assert gateway.run_path.read_bytes() == original
    assert old_backup.read_text() == "# Obsolete monolithic gateway\n"


def test_cli_install_preflights_both_targets(installation, tmp_path, capsys):
    gateway, cron = installation
    cron.cron_path.write_text("# Missing delivery seams\n", encoding="utf-8")
    before = snapshot(tmp_path)
    with patch.object(cli, "_get_patcher", return_value=gateway), \
            patch.object(cli, "_get_cron_patcher", return_value=cron):
        assert cli._cmd_install() == 1
    assert "Patch failed" in capsys.readouterr().out
    assert snapshot(tmp_path) == before


def test_cli_verify_status_and_transactional_remove(installation, capsys):
    gateway, cron = installation
    with patch.object(cli, "_get_patcher", return_value=gateway), \
            patch.object(cli, "_get_cron_patcher", return_value=cron):
        assert cli._cmd_verify() == 0
        assert "split gateway modules" in capsys.readouterr().out
        assert cli._cmd_install() == 0
        with patch.object(patcher_module, "hermes_python", return_value=None), \
                patch.object(patcher_module, "hermes_install_dir", return_value=None):
            assert cli._cmd_status() == 0
        output = capsys.readouterr().out
        assert "Fully patched: yes" in output
        assert "run_turn_runner.py:" in output
        assert "Cron fully patched: yes" in output
        assert cli._cmd_uninstall() == 0
    assert not gateway.is_patched()
    assert not cron.is_patched()


@pytest.mark.parametrize("operation", ["_cmd_uninstall", "_cmd_restore"])
def test_cli_recovery_write_failure_preserves_installation(installation, tmp_path, operation):
    gateway, cron = installation
    install_patchers([gateway, cron])
    before = snapshot(tmp_path)
    failed = False

    def fail_once(path, content):
        nonlocal failed
        if path == cron.cron_path and not failed:
            failed = True
            raise OSError("simulated recovery failure")
        _atomic_write(path, content)

    with (
        patch.object(cli, "_get_patcher", return_value=gateway),
        patch.object(cli, "_get_cron_patcher", return_value=cron),
        patch.object(patcher_module, "_atomic_write", side_effect=fail_once),
    ):
        assert getattr(cli, operation)() == 1
    assert failed
    assert snapshot(tmp_path) == before
