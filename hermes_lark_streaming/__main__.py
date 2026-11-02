"""只读诊断入口，安装和启停交给 Hermes 插件管理器."""

from __future__ import annotations

import sys
from pathlib import Path

from . import __version__


def legacy_files() -> list[str]:
    """只读检测旧注入，不解析 AST 或恢复历史备份."""
    import hermes_cli

    root = Path(hermes_cli.__file__).resolve().parent.parent
    names = [
        "gateway/run.py",
        "gateway/run_inbound.py",
        "gateway/run_turn.py",
        "gateway/run_turn_runner.py",
        "gateway/run_busy.py",
        "gateway/run_notifications.py",
        "cron/scheduler.py",
        "cron/scheduler_delivery.py",
    ]
    return [name for name in names if (root / name).is_file() and "HERMES_LARK_" in (root / name).read_text()]


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    if command == "help":
        print("hermes-lark-streaming verify | status | version")
        print("Install/enable/disable with `hermes plugins`; legacy patch commands were removed.")
        return 0
    if command == "version":
        print(__version__)
        return 0
    if command not in {"verify", "status"}:
        print("Unsupported command. Remove old injections with the OLD release before upgrading.")
        return 2
    try:
        from .plugin import check_host

        check_host()
    except (ImportError, RuntimeError) as exc:
        print(f"Verification failed: {exc}")
        return 1
    print(f"hermes-lark-streaming {__version__}: native platform interfaces available; no old injections found")
    print("This verifies local interfaces only; it does not send a Feishu message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
