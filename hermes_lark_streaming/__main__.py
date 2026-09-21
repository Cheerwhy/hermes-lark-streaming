"""CLI 入口: python -m hermes_lark_streaming [install|uninstall|status|verify]。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .patcher import CronPatcher, Patcher


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _print_usage()
        return 0

    cmd = args[0]
    commands = _commands()
    handler = commands.get(cmd)
    if handler is not None:
        return handler()

    print(f"Unknown command: {cmd}")
    _print_usage()
    return 1


def _commands() -> dict[str, Callable[[], int]]:
    return {
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
        "restore": _cmd_restore,
        "status": _cmd_status,
        "verify": _cmd_verify,
    }


def _print_usage() -> None:
    print("Usage: python -m hermes_lark_streaming <command>")
    print()
    print("Commands:")
    print("  install    Apply AST hooks to Hermes gateway and cron modules")
    print("  uninstall  Remove AST patch")
    print("  restore    Restore from backup")
    print("  status     Show current patch status")
    print("  verify     Verify compatibility without patching")


def _get_patcher() -> Patcher | None:
    from .patcher import Patcher, PatcherError

    try:
        return Patcher()
    except PatcherError as e:
        print(f"Error: {e}")
        return None


def _get_cron_patcher() -> CronPatcher | None:
    from .patcher import CronPatcher, PatcherError

    try:
        return CronPatcher()
    except PatcherError:
        return None


def _cmd_install() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import install_patchers

    patchers: list[Patcher | CronPatcher] = [patcher]
    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        patchers.append(cron_patcher)
    print("Verifying and preparing all gateway/cron hooks before writing...")
    try:
        install_patchers(patchers)
    except Exception as e:
        print(f"Patch failed: {e}")
        return 1
    print("Hooks installed. Restart Hermes Gateway to load the changes.")
    return 0


def _cmd_uninstall() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import _write_changes

    print("Removing patch...")
    try:
        changes = patcher.prepare_remove()
        cron_patcher = _get_cron_patcher()
        if cron_patcher is not None:
            changes.update(cron_patcher.prepare_remove())
        _write_changes(changes)
    except Exception as e:
        print(f"Remove failed: {e}")
        return 1
    print("Patch removed.")
    return 0


def _cmd_restore() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    from .patcher import _BACKUP_SUFFIX, _write_changes

    print("Restoring from backup...")
    try:
        changes = patcher.prepare_restore()
        cron_patcher = _get_cron_patcher()
        if cron_patcher is not None:
            backup = cron_patcher.cron_path.with_suffix(cron_patcher.cron_path.suffix + _BACKUP_SUFFIX)
            if backup.exists() or cron_patcher.is_patched():
                changes.update(cron_patcher.prepare_restore())
        _write_changes(changes)
    except Exception as e:
        print(f"Restore failed: {e}")
        return 1
    print("Restored.")
    return 0


def _cmd_status() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    patched = patcher.is_patched()
    print(f"Patched: {'yes' if patched else 'no'}")
    print(f"Target:  {patcher.run_path}")
    print(f"Layout:  {'split gateway modules' if patcher.split else 'monolithic gateway'}")

    if patched:
        print(f"Fully patched: {'yes' if patcher.is_fully_patched() else 'no'}")
    for path in patcher.target_paths:
        if not path.exists():
            print(f"  {path.name}: MISSING FILE")
            continue
        content = path.read_text(encoding="utf-8")
        labels = [
            begin.replace("# HERMES_LARK_", "").replace("_BEGIN", "").lower()
            for begin, _end in patcher.MARKERS if begin in content
        ]
        print(f"  {path.name}: {', '.join(labels) if labels else 'no hooks'}")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        print(f"Cron hook: {'installed' if cron_patcher.is_patched() else 'not installed'}")
        print(f"Cron target: {cron_patcher.cron_path}")
        if cron_patcher.is_patched():
            print(f"Cron fully patched: {'yes' if cron_patcher.is_fully_patched() else 'no'}")

    # Check config
    from .config import Config

    cfg = Config()
    print(f"Config streaming.enabled: {cfg.enabled}")
    print(f"Feishu credentials: {'configured' if (cfg.env_app_id or cfg.feishu_app_id) else 'MISSING'}")

    # Python interpreter check
    from .patcher import hermes_install_dir, hermes_python

    expected_py = hermes_python()
    if expected_py is not None:
        print(f"Hermes Python: {expected_py}")
        current = Path(sys.executable).resolve()
        if current != expected_py.resolve():
            print(f"  warning: running under {current}, but Hermes uses {expected_py}")
            print(f"  rerun commands with: {expected_py} -m hermes_lark_streaming ...")

    install_dir = hermes_install_dir()
    if install_dir is not None:
        print(f"Hermes install dir: {install_dir}")
    return 0


def _cmd_verify() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    print(f"Target: {patcher.run_path}")
    if patcher.split:
        print("Layout: split gateway modules")
        for path in patcher.target_paths[1:]:
            print(f"  {path}")
    print("Checking compatibility...")
    try:
        patcher.verify_target()
    except Exception as e:
        print(f"Incompatible: {e}")
        return 1
    print("Compatible.")

    cron_patcher = _get_cron_patcher()
    if cron_patcher is not None:
        print(f"Cron target: {cron_patcher.cron_path}")
        try:
            cron_patcher.verify_target()
        except Exception as e:
            print(f"Cron incompatible: {e}")
            return 1
        print("Cron target compatible.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
