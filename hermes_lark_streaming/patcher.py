"""AST Patcher — 在 Hermes gateway/run.py 中注入 Hook 调用."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

from .config import hermes_home

_logger = logging.getLogger("hermes_lark_streaming")


PREFIX = "HERMES_LARK"

_HOOK_NAMES = [
    "NORMALIZE",
    "START",
    "COMPLETE",
    "FOLLOWUP_COMPLETE",
    "FOLLOWUP_RESULT",
    "TOOL",
    "ANSWER",
    "THINKING",
    "REASONING",
    "BACKGROUND_REVIEW",
    "ABORT",
    "STOP",
    "INTERRUPT",
    "BG_DELIVER",
    "CLARIFY",
]
MARKERS: list[tuple[str, str]] = [(f"# {PREFIX}_{n}_BEGIN", f"# {PREFIX}_{n}_END") for n in _HOOK_NAMES]

MK_NORMALIZE, MK_NORMALIZE_END = MARKERS[0]
MK_START, MK_START_END = MARKERS[1]
MK_REASONING, MK_REASONING_END = MARKERS[8]
MK_BACKGROUND_REVIEW, MK_BACKGROUND_REVIEW_END = MARKERS[9]
MK_ABORT, MK_ABORT_END = MARKERS[10]
MK_STOP, MK_STOP_END = MARKERS[11]
MK_BG_DELIVER, MK_BG_DELIVER_END = MARKERS[13]
MK_CLARIFY, MK_CLARIFY_END = MARKERS[14]

_BACKUP_SUFFIX = ".hermes_lark.bak"


def _valid_source(path: Path) -> Path | None:
    try:
        candidate = path.resolve()
        if candidate.is_file() and candidate.suffix == ".py":
            return candidate
    except (OSError, RuntimeError):
        _logger.debug("Invalid Hermes source candidate: %s", path, exc_info=True)
    return None


def _module_to_path(module_name: str) -> Path:
    """gateway.run → gateway/run.py."""
    return Path(*module_name.split(".")).with_suffix(".py")


# 候选代码根目录，按优先级排列（来源: Hermes 官方安装文档 Install Layout）。
# - per-user git installer: <HERMES_HOME>/hermes-agent/
# - root-mode (sudo curl|bash): /usr/local/lib/hermes-agent/
def _code_roots() -> list[Path]:
    return [hermes_home() / "hermes-agent", Path("/usr/local/lib/hermes-agent")]


# venv 内 python 解释器候选（覆盖 venv/.venv 命名变体）。
_VENV_PYTHONS: tuple[tuple[str, ...], ...] = (
    ("venv", "bin", "python3"),
    ("venv", "bin", "python"),
    (".venv", "bin", "python3"),
    (".venv", "bin", "python"),
)


def _python_from_hermes_cli() -> Path | None:
    """从 which hermes 反推 venv 内的 python3。"""
    cli = shutil.which("hermes")
    if cli is None:
        return None
    cli_path = Path(cli)
    # 读脚本内容，依次试 exec 行(bash wrapper) 和 shebang(console_scripts)
    try:
        text = cli_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # 1. bash wrapper: exec "venv/bin/hermes" → 同目录 python3
    m = re.search(r'''exec\s+["']([^"']+)["']''', text)
    if m:
        venv_bin = Path(m.group(1)).parent  # venv/bin
        for name in ("python3", "python"):
            py = venv_bin / name
            if py.exists():
                return py
    # 2. console_scripts: shebang #!/path/to/python3 直接指向 python
    m = re.match(r'^#!\s*(\S+)', text)
    if m:
        py = Path(m.group(1))
        if py.exists() and "python" in py.name.lower():
            return py
    return None


def hermes_python() -> Path | None:
    """定位 Hermes 的 Python: which hermes 优先, _code_roots 兜底."""
    # 1. which hermes (覆盖所有官方安装方式，跨平台)
    if py := _python_from_hermes_cli():
        return py
    # 2. 兜底: 已知代码根下的 venv
    for root in _code_roots():
        for parts in _VENV_PYTHONS:
            py = root.joinpath(*parts)
            if py.exists():
                return py
    return None


def hermes_install_dir() -> Path | None:
    """定位 Hermes 安装目录 (含 gateway/run.py): hermes_constants 优先, _code_roots 兜底."""
    # 1. 用 Hermes Python 调用官方 API (single source of truth)
    py = hermes_python()
    if py is not None:
        try:
            result = subprocess.run(
                [str(py), "-c", "from hermes_constants import get_hermes_home; print(get_hermes_home())"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            _logger.debug("hermes_constants lookup failed", exc_info=True)
        else:
            if result.returncode == 0:
                home = Path(result.stdout.strip())
                install = home / "hermes-agent"
                if install.exists():
                    return install
    # 2. 兜底: _code_roots 里含 gateway/run.py 的那个
    rel = _module_to_path("gateway.run")
    for root in _code_roots():
        if (root / rel).exists():
            return root
    return None


def _resolve_module_path(module_name: str, roots: list[Path]) -> Path:
    """定位 Hermes 模块文件，候选代码根优先，importlib 兜底."""
    rel = _module_to_path(module_name)
    for root in roots:
        if candidate := _valid_source(root / rel):
            return candidate

    package = module_name.partition(".")[0]
    try:
        spec = importlib.util.find_spec(package)
        locations = spec.submodule_search_locations if spec else None
        # submodule_search_locations 指向包目录（如 .../gateway），
        # 因此需剥掉包名前缀，得到包内子路径。
        in_pkg = rel.relative_to(Path(package)) if rel.parts[0] == package else rel
        for location in locations or []:
            if candidate := _valid_source(Path(location) / in_pkg):
                return candidate
    except Exception:
        _logger.debug("Failed to resolve Hermes module %s", module_name, exc_info=True)
    return roots[0] / rel if roots else rel


def _default_run_path() -> Path:
    return _resolve_module_path("gateway.run", _code_roots())


def _default_cron_path() -> Path:
    return _resolve_module_path("cron.scheduler", _code_roots())


MK_CRON_DELIVER = f"# {PREFIX}_CRON_DELIVER_BEGIN"
MK_CRON_DELIVER_END = f"# {PREFIX}_CRON_DELIVER_END"

def _make_hook(indent: str, begin: str, end: str, body_lines: list[str]) -> str:
    return f"{indent}{begin}\n" + "".join(f"{indent}{line}\n" for line in body_lines) + f"{indent}{end}\n"


def _hook_exception_lines(hook_name: str, indent: str = "") -> list[str]:
    return [
        f"{indent}except Exception:",
        f"{indent}    import logging as _lark_logging",
        f'{indent}    _lark_logging.getLogger("hermes_lark_streaming").exception('
        f'"injected hook failed: {hook_name}")',
    ]


def _feishu_normalize_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_NORMALIZE,
        MK_NORMALIZE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_feishu_normalize",
            "    on_feishu_normalize(",
            "        message_id=event.message_id,",
            "        source=source,",
            "        event=event,",
            "        reply_anchor_id=self._reply_anchor_for_event(event),",
            "    )",
            *_hook_exception_lines("normalize"),
        ],
    )


def _start_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_START,
        MK_START_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import on_message_started",
            "        _lark_anchor_id = self._reply_anchor_for_event(event)",
            "        on_message_started(",
            "            message_id=event.message_id,",
            "            chat_id=source.chat_id,",
            "            anchor_id=_lark_anchor_id,",
            "            session_key=locals().get('session_key') or locals().get('_quick_key'),",
            "        )",
            *_hook_exception_lines("start"),
        ],
    )


def _complete_body(indent: str) -> str:
    lines = [
        "try:",
        "    from hermes_lark_streaming.patch import on_message_completed_wait, on_message_needs_text_fallback",
        "    _lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id",
        "    _lark_card_sent = await on_message_completed_wait(",
        "        message_id=_lark_completion_id,",
        "        answer=_lark_completion_answer,",
        "        is_error=bool(agent_result.get('failed')),",
        "        reconcile_answer=bool(agent_result.get('failed') or agent_result.get('response_transformed')",
        "                             or _lark_completion_answer != _lark_original_response),",
        "        duration=_turn_seconds,",
        "        model=agent_result.get('model', ''),",
        "        tokens={",
        "            'input_tokens': agent_result.get('input_tokens', 0),",
        "            'output_tokens': agent_result.get('output_tokens', 0),",
        "        },",
        "        context={",
        "            'used_tokens': agent_result.get('last_prompt_tokens', 0),",
        "            'max_tokens': agent_result.get('context_length', 0),",
        "        },",
        "    )",
        "    if _lark_card_sent:",
        "        agent_result['already_sent'] = True",
        "        _footer_line = ''",
        "        if agent_result.get('failed'):",
        "            response = ''",
        "    elif on_message_needs_text_fallback(message_id=_lark_completion_id):",
        "        agent_result.pop('already_sent', None)",
        *_hook_exception_lines("complete"),
    ]
    return "".join(f"{indent}{line}\n" for line in lines)


def _reasoning_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_REASONING,
        MK_REASONING_END,
        [
            "def _reasoning_cb(text):",
            "    try:",
            "        _lark_message_id = ctx.event_message_id",
            "        _lark_run_current = ctx._run_still_current",
            "        if text and _lark_run_current():",
            "            from hermes_lark_streaming.patch import on_reasoning_delta",
            "            on_reasoning_delta(message_id=_lark_message_id, text=text)",
            *_hook_exception_lines("reasoning", indent="    "),
            "agent.reasoning_callback = _reasoning_cb",
        ],
    )


def _background_review_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_BACKGROUND_REVIEW,
        MK_BACKGROUND_REVIEW_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_background_review_message",
            "    _lark_bg_review_sender = agent.background_review_callback",
            "    def _lark_bg_review_callback(message):",
            "        _lark_message_id = ctx.event_message_id",
            "        _lark_bg_review_deferred = on_background_review_message(",
            "            message_id=_lark_message_id,",
            "            text=message,",
            "            sender=_lark_bg_review_sender,",
            "        )",
            "        if not _lark_bg_review_deferred:",
            "            _lark_bg_review_sender(message)",
            "    agent.background_review_callback = _lark_bg_review_callback",
            *_hook_exception_lines("background_review"),
        ],
    )


def _abort_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_ABORT,
        MK_ABORT_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_aborted",
            "    on_message_aborted(message_id=event.message_id)",
            *_hook_exception_lines("abort"),
        ],
    )


def _stop_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_STOP,
        MK_STOP_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark'):",
            "        from hermes_lark_streaming.patch import on_session_aborted",
            "        await on_session_aborted(",
            "            session_key=locals().get('quick_key') or locals().get('_quick_key') or '',",
            "        )",
            *_hook_exception_lines("stop"),
        ],
    )


def _bg_deliver_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_BG_DELIVER,
        MK_BG_DELIVER_END,
        [
            "try:",
            "    if source.platform.value.lower() in ('feishu', 'lark') and response:",
            "        from hermes_lark_streaming.patch import on_background_deliver",
            "        _bg_preview = prompt[:60] + ('...' if len(prompt) > 60 else '')",
            "        if await on_background_deliver(",
            "            chat_id=source.chat_id,",
            "            preview=_bg_preview,",
            "            content=text_content,",
            "            reply_to_message_id=event_message_id,",
            "        ):",
            "            text_content = ''",
            "            if not images and not media_files:",
            "                return",
            *_hook_exception_lines("background_deliver"),
        ],
    )


def _clarify_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_CLARIFY,
        MK_CLARIFY_END,
        [
            "try:",
            "    import functools",
            "    from hermes_lark_streaming.patch import on_clarify_enter, on_clarify_exit",
            "    _lark_clarify_orig = agent.clarify_callback",
            "    @functools.wraps(_lark_clarify_orig)",
            "    def _lark_clarify_wrapper(*args, **kwargs):",
            "        try:",
            "            _lark_clarify_msg_id = ctx.event_message_id",
            "            _lark_clarify_chat_id = ctx._status_chat_id",
            "            _lark_clarify_sk = ctx.session_key",
            "        except Exception:",
            "            _lark_clarify_msg_id = None",
            "            _lark_clarify_chat_id = None",
            "            _lark_clarify_sk = None",
            "        on_clarify_enter(",
            "            message_id=_lark_clarify_msg_id,",
            "            chat_id=_lark_clarify_chat_id,",
            "            session_key=_lark_clarify_sk,",
            "        )",
            "        try:",
            "            return _lark_clarify_orig(*args, **kwargs)",
            "        finally:",
            "            on_clarify_exit(",
            "                message_id=_lark_clarify_msg_id,",
            "                chat_id=_lark_clarify_chat_id,",
            "                session_key=_lark_clarify_sk,",
            "            )",
            "    agent.clarify_callback = _lark_clarify_wrapper",
            *_hook_exception_lines("clarify"),
        ],
    )


def _remove_block(content: str, begin: str, end: str) -> str:
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == begin:
            if in_block:
                return content
            in_block = True
            continue
        if stripped == end:
            if not in_block:
                return content
            in_block = False
            continue
        if not in_block:
            result.append(line)
    return content if in_block else "".join(result)


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件再 rename，防止崩溃时文件损坏."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, dir=str(path.parent), prefix=".hermes_lark_", mode="w", encoding="utf-8", newline=""
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
        shutil.copymode(path, tmp_path)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


class PatcherError(RuntimeError):
    pass


def _remove_block_checked(content: str, begin: str, end: str) -> str:
    updated = _remove_block(content, begin, end)
    if any(line.strip() in (begin, end) for line in updated.splitlines()):
        raise PatcherError(f"Malformed injected marker block: {begin}")
    return updated


def _clean_hooks(content: str, markers: list[tuple[str, str]]) -> str:
    for begin, end in markers:
        content = _remove_block_checked(content, begin, end)
    return content


def _read_source(path: Path) -> str:
    """Keep original line endings for backups, removal, and transaction rollback."""
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _write_changes(changes: dict[Path, str]) -> None:
    """Roll back earlier replacements if any file in a prepared operation fails."""
    originals = {path: _read_source(path) if path.exists() else None for path in changes}
    written: list[Path] = []
    try:
        for path, content in changes.items():
            if originals[path] == content:
                continue
            if originals[path] is None:
                # Backups are new files; source replacements retain their mode.
                path.touch(exist_ok=False)
            written.append(path)
            _atomic_write(path, content)
    except BaseException:
        rollback_errors = []
        for path in reversed(written):
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, original)
            except OSError as exc:
                rollback_errors.append(f"{path}: {exc}")
        if rollback_errors:
            _logger.error("Patch rollback incomplete: %s", "; ".join(rollback_errors))
        raise


def install_patchers(patchers: list[Patcher | CronPatcher]) -> None:
    """Preflight gateway and cron together before changing sources or backups."""
    changes: dict[Path, str] = {}
    for patcher in patchers:
        prepared = patcher.prepare_install()
        for path, content in prepared.items():
            original = _read_source(path)
            if original == content and not any(
                marker in content for pair in patcher.MARKERS for marker in pair
            ):
                continue
            clean = _clean_hooks(original, patcher.MARKERS)
            backup = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
            # Refresh stale backups after an upstream upgrade, never restore old code over it.
            changes[backup] = clean
            changes[path] = content
    _write_changes(changes)


def _prepare_restore(paths: list[Path], markers: list[tuple[str, str]]) -> dict[Path, str]:
    changes = {}
    for path in paths:
        backup = path.with_suffix(path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            if any(marker in _read_source(path) for pair in markers for marker in pair):
                raise PatcherError(f"No backup found: {backup}")
            continue
        content = _read_source(backup)
        current = _clean_hooks(_read_source(path), markers)
        if current != content:
            raise PatcherError(f"Backup no longer matches upstream source: {backup}; use uninstall instead")
        compile(content, str(path), "exec")
        changes[path] = content
    if not changes:
        raise PatcherError("No backup found for current Hermes targets")
    return changes


class Patcher:
    """管理 AST 注入的安装和移除."""

    MARKERS: list[tuple[str, str]] = MARKERS

    def __init__(self, run_path: Path | None = None) -> None:
        self.run_path = run_path or _default_run_path()
        if not self.run_path.exists():
            tried = ", ".join(str(r) for r in _code_roots())
            raise PatcherError(
                f"gateway/run.py not found: {self.run_path} "
                f"(tried: {tried}). "
                f"Set HERMES_HOME to the dir containing hermes-agent/ and rerun."
            )

        tree = ast.parse(_clean_hooks(_read_source(self.run_path), self.MARKERS))
        monolithic = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_handle_message_with_agent"
            for node in ast.walk(tree)
        )
        if monolithic or not (self.run_path.parent / "run_turn.py").exists():
            raise PatcherError(
                "Unsupported Hermes gateway layout: expected the split gateway modules "
                "(gateway/run_turn.py et al). Hermes >= 0.21.1 (v2026.9.7) is required; "
                "the monolithic gateway was removed in that release."
            )

    @property
    def target_paths(self) -> list[Path]:
        from .split_gateway import GATEWAY_FILES

        return [self.run_path, *(self.run_path.parent / name for name in GATEWAY_FILES)]

    def is_patched(self) -> bool:
        return any(
            marker in _read_source(path)
            for path in self.target_paths if path.exists()
            for pair in self.MARKERS for marker in pair
        )

    def is_fully_patched(self) -> bool:
        try:
            return all(
                _read_source(path) == content
                for path, content in self.prepare_install().items()
            )
        except (PatcherError, SyntaxError, OSError):
            return False

    def verify_target(self) -> None:
        self.prepare_install()

    def prepare_install(self) -> dict[Path, str]:
        from .split_gateway import inject_gateway

        changes = {}
        for path in self.target_paths:
            if not path.is_file():
                raise PatcherError(f"Missing split gateway module: {path}")
            content = _clean_hooks(_read_source(path), self.MARKERS)
            updated = content if path == self.run_path else inject_gateway(path.name, content)
            compile(updated, str(path), "exec")
            changes[path] = updated
        return changes

    def apply(self) -> None:
        install_patchers([self])

    def remove(self) -> None:
        _write_changes(self.prepare_remove())

    def prepare_remove(self) -> dict[Path, str]:
        changes = {}
        for path in self.target_paths:
            if path.exists():
                content = _clean_hooks(_read_source(path), self.MARKERS)
                compile(content, str(path), "exec")
                changes[path] = content
        return changes

    def restore(self) -> None:
        _write_changes(self.prepare_restore())

    def prepare_restore(self) -> dict[Path, str]:
        # A pre-upgrade run.py backup holds the old monolithic file; only the
        # split modules carry injected hooks and therefore a restorable backup.
        paths = [path for path in self.target_paths if path != self.run_path]
        return _prepare_restore(paths, self.MARKERS)


class CronPatcher:
    """注入 CRON_DELIVER hook 到 cron/scheduler.py 的 _deliver_result."""

    MARKERS: ClassVar[list[tuple[str, str]]] = [(MK_CRON_DELIVER, MK_CRON_DELIVER_END)]

    def __init__(self, cron_path: Path | None = None) -> None:
        self.cron_path = cron_path or _default_cron_path()
        if not self.cron_path.exists():
            tried = ", ".join(str(r) for r in _code_roots())
            raise PatcherError(
                f"cron/scheduler.py not found: {self.cron_path} "
                f"(tried: {tried}). "
                f"Set HERMES_HOME to the dir containing hermes-agent/ and rerun."
            )
        delivery_path = self.cron_path.with_name("scheduler_delivery.py")
        if not delivery_path.is_file():
            raise PatcherError(
                f"Missing split cron module: {delivery_path}. "
                "Hermes >= 0.21.1 (v2026.9.7) is required; the monolithic "
                "cron/scheduler.py delivery lane was removed in that release."
            )
        self.cron_path = delivery_path

    def is_patched(self) -> bool:
        content = _read_source(self.cron_path)
        return any(marker in content for pair in self.MARKERS for marker in pair)

    def is_fully_patched(self) -> bool:
        try:
            return _read_source(self.cron_path) == self.prepare_install()[self.cron_path]
        except (PatcherError, SyntaxError, OSError):
            return False

    def verify_target(self) -> None:
        self.prepare_install()

    def apply(self) -> None:
        install_patchers([self])

    def prepare_install(self) -> dict[Path, str]:
        from .split_cron import inject_cron

        content = _clean_hooks(_read_source(self.cron_path), self.MARKERS)
        updated = inject_cron(content)
        compile(updated, str(self.cron_path), "exec")
        return {self.cron_path: updated}

    def remove(self) -> None:
        _write_changes(self.prepare_remove())

    def prepare_remove(self) -> dict[Path, str]:
        content = _clean_hooks(_read_source(self.cron_path), self.MARKERS)
        compile(content, str(self.cron_path), "exec")
        return {self.cron_path: content}

    def restore(self) -> None:
        _write_changes(self.prepare_restore())

    def prepare_restore(self) -> dict[Path, str]:
        return _prepare_restore([self.cron_path], self.MARKERS)
