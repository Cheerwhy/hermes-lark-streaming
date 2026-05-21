"""CLI 入口: python -m hermes_lark_streaming [install|uninstall|status|verify]。"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .patcher import Patcher


def main() -> int:
    args = sys.argv[1:]
    if not args:
        _print_usage()
        return 0

    cmd = args[0]

    if cmd == "install":
        return _cmd_install()
    if cmd == "uninstall":
        return _cmd_uninstall()
    if cmd == "status":
        return _cmd_status()
    if cmd == "verify":
        return _cmd_verify()
    if cmd == "restore":
        return _cmd_restore()

    print(f"Unknown command: {cmd}")
    _print_usage()
    return 1


def _print_usage() -> None:
    print("Usage: python -m hermes_lark_streaming <command>")
    print()
    print("Commands:")
    print("  install    Apply AST patch to gateway/run.py")
    print("  uninstall  Remove AST patch from gateway/run.py")
    print("  restore    Restore run.py from backup")
    print("  status     Show current patch status")
    print("  verify     Verify run.py compatibility without patching")


def _get_patcher() -> Patcher | None:
    from .patcher import Patcher, PatcherError

    try:
        return Patcher()
    except PatcherError as e:
        print(f"Error: {e}")
        return None


def _cmd_install() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    if patcher.is_fully_patched():
        print("Already patched.")
        return 0

    print("Verifying target compatibility...")
    try:
        patcher.verify_target()
    except Exception as e:
        print(f"Verification failed: {e}")
        return 1
    print("Target compatible.")

    print("Applying patch...")
    try:
        patcher.apply()
    except Exception as e:
        print(f"Patch failed: {e}")
        return 1
    print("Patch applied successfully.")
    return 0


def _cmd_uninstall() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    if not patcher.is_patched():
        print("Not patched.")
        return 0

    print("Removing patch...")
    try:
        patcher.remove()
    except Exception as e:
        print(f"Remove failed: {e}")
        return 1
    print("Patch removed.")
    return 0


def _cmd_restore() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    print("Restoring from backup...")
    try:
        patcher.restore()
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

    if patched:
        from .patcher import Patcher as _PatcherCls

        content = patcher.run_path.read_text(encoding="utf-8")
        for begin, _end in _PatcherCls.MARKERS:
            found = begin in content
            label = begin.replace("# HERMES_LARK_", "").replace("_BEGIN", "").lower()
            print(f"  {label}: {'installed' if found else 'missing'}")

    # Check config
    from .config import Config

    cfg = Config()
    print(f"Config streaming.enabled: {cfg.enabled}")
    print(f"Feishu credentials: {'configured' if (cfg.env_app_id or cfg.feishu_app_id) else 'MISSING'}")
    return 0


def _cmd_verify() -> int:
    patcher = _get_patcher()
    if patcher is None:
        return 1

    print(f"Target: {patcher.run_path}")
    print("Checking compatibility...")
    try:
        patcher.verify_target()
    except Exception as e:
        print(f"Incompatible: {e}")
        return 1
    print("Compatible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
