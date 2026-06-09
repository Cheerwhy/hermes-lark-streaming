"""AST Patcher — 在 Hermes gateway/run.py 中注入 Hook 调用."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import logging
import os
import shutil
import tempfile
from pathlib import Path

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
    "INTERRUPT",
    "BG_DELIVER",
]
MARKERS: list[tuple[str, str]] = [(f"# {PREFIX}_{n}_BEGIN", f"# {PREFIX}_{n}_END") for n in _HOOK_NAMES]

MK_NORMALIZE, MK_NORMALIZE_END = MARKERS[0]
MK_START, MK_START_END = MARKERS[1]
MK_COMPLETE, MK_COMPLETE_END = MARKERS[2]
MK_FOLLOWUP_COMPLETE, MK_FOLLOWUP_COMPLETE_END = MARKERS[3]
MK_FOLLOWUP_RESULT, MK_FOLLOWUP_RESULT_END = MARKERS[4]
MK_TOOL, MK_TOOL_END = MARKERS[5]
MK_ANSWER, MK_ANSWER_END = MARKERS[6]
MK_THINKING, MK_THINKING_END = MARKERS[7]
MK_REASONING, MK_REASONING_END = MARKERS[8]
MK_BACKGROUND_REVIEW, MK_BACKGROUND_REVIEW_END = MARKERS[9]
MK_ABORT, MK_ABORT_END = MARKERS[10]
MK_INTERRUPT, MK_INTERRUPT_END = MARKERS[11]
MK_BG_DELIVER, MK_BG_DELIVER_END = MARKERS[12]

_BACKUP_SUFFIX = ".hermes_lark.bak"

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _resolve_module_path(module_name: str, hardcoded: Path) -> Path:
    """Discover the actual file path of a Hermes module.

    Works for both git-clone (editable install) and pip-install scenarios.
    Resolves dotted modules without importing their parent package, then
    falls back to the hardcoded path so Patcher can report a clear error.
    """
    try:
        package_name, separator, relative_name = module_name.partition(".")
        spec = importlib.util.find_spec(package_name)
        if separator and spec and spec.submodule_search_locations:
            relative_path = Path(*relative_name.split(".")).with_suffix(".py")
            for location in spec.submodule_search_locations:
                candidate = (Path(location) / relative_path).resolve()
                if candidate.is_file():
                    return candidate
        elif spec and spec.origin:
            candidate = Path(spec.origin).resolve()
            if candidate.is_file() and candidate.suffix == ".py":
                return candidate
    except Exception:
        _logger.debug("Failed to resolve Hermes module %s", module_name, exc_info=True)
    return hardcoded


_RUN_PATH = _resolve_module_path(
    "gateway.run", _HERMES_HOME / "hermes-agent" / "gateway" / "run.py"
)
_CRON_PATH = _resolve_module_path(
    "cron.scheduler", _HERMES_HOME / "hermes-agent" / "cron" / "scheduler.py"
)

MK_CRON_DELIVER = f"# {PREFIX}_CRON_DELIVER_BEGIN"
MK_CRON_DELIVER_END = f"# {PREFIX}_CRON_DELIVER_END"


def _make_hook(indent: str, begin: str, end: str, body_lines: list[str]) -> str:
    return f"{indent}{begin}\n" + "".join(f"{indent}{line}\n" for line in body_lines) + f"{indent}{end}\n"


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
            "except Exception:",
            "    pass",
        ],
    )


def _start_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_START,
        MK_START_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_started",
            "    _lark_anchor_id = self._reply_anchor_for_event(event)",
            "    on_message_started(",
            "        message_id=event.message_id,",
            "        chat_id=source.chat_id,",
            "        anchor_id=_lark_anchor_id,",
            "    )",
            "except Exception:",
            "    pass",
        ],
    )


def _complete_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_COMPLETE,
        MK_COMPLETE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_completed_wait, on_message_needs_text_fallback",
            "    _lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id",
            "    _lark_card_sent = await on_message_completed_wait(",
            "        message_id=_lark_completion_id,",
            "        answer=response,",
            "        duration=_response_time,",
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
            "    elif on_message_needs_text_fallback(message_id=_lark_completion_id):",
            "        agent_result.pop('already_sent', None)",
            "except Exception:",
            "    pass",
        ],
    )


def _followup_complete_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_FOLLOWUP_COMPLETE,
        MK_FOLLOWUP_COMPLETE_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_queued_followup_boundary",
            "    await on_queued_followup_boundary(message_id=event_message_id, result=result)",
            "except Exception:",
            "    pass",
        ],
    )


def _followup_result_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_FOLLOWUP_RESULT,
        MK_FOLLOWUP_RESULT_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_queued_followup_result",
            "    _lark_followup_completion_id = next_message_id or getattr(pending_event, 'message_id', None)",
            "    if _lark_followup_completion_id:",
            "        on_queued_followup_result(",
            "            message_id=_lark_followup_completion_id,",
            "            followup_result=followup_result,",
            "        )",
            "except Exception:",
            "    pass",
        ],
    )


def _tool_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_TOOL,
        MK_TOOL_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_tool_updated",
            "    if _run_still_current() and event_type in ('tool.started', 'tool.completed'):",
            "        if on_tool_updated(",
            "            message_id=event_message_id,",
            "            tool_name=tool_name or '',",
            "            status='started' if event_type == 'tool.started' else 'completed',",
            "            detail=preview or '',",
            "        ):",
            "            return",
            "except Exception:",
            "    pass",
        ],
    )


def _answer_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_ANSWER,
        MK_ANSWER_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_answer_delta",
            "    if text and _run_still_current() and on_answer_delta(message_id=event_message_id, text=text):",
            "        return",
            "except Exception:",
            "    pass",
        ],
    )


def _thinking_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_THINKING,
        MK_THINKING_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_thinking_delta",
            "    if (text and not already_streamed and _run_still_current()",
            "            and on_thinking_delta(message_id=event_message_id, text=text)):",
            "        return",
            "except Exception:",
            "    pass",
        ],
    )


def _reasoning_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_REASONING,
        MK_REASONING_END,
        [
            "def _reasoning_cb(text):",
            "    if text and _run_still_current():",
            "        try:",
            "            from hermes_lark_streaming.patch import on_reasoning_delta",
            "            on_reasoning_delta(message_id=event_message_id, text=text)",
            "        except Exception:",
            "            pass",
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
            "        _lark_bg_review_deferred = on_background_review_message(",
            "            message_id=event_message_id,",
            "            text=message,",
            "            sender=_lark_bg_review_sender,",
            "        )",
            "        if not _lark_bg_review_deferred:",
            "            _lark_bg_review_sender(message)",
            "    agent.background_review_callback = _lark_bg_review_callback",
            "except Exception:",
            "    pass",
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
            "except Exception:",
            "    pass",
        ],
    )


def _interrupt_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_INTERRUPT,
        MK_INTERRUPT_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import (",
            "        on_message_aborted, on_message_interrupted, on_message_started,",
            "    )",
            "    _lark_next_message_id = getattr(pending_event, 'message_id', None) or next_message_id",
            "    _lark_next_anchor_id = next_message_id",
            "    if was_interrupted and _lark_next_message_id:",
            "        on_message_interrupted(",
            "            message_id=event_message_id,",
            "            new_message_id=_lark_next_message_id,",
            "            chat_id=source.chat_id,",
            "            anchor_id=_lark_next_anchor_id,",
            "        )",
            "    elif was_interrupted:",
            "        on_message_aborted(message_id=event_message_id)",
            "    elif pending_event is not None and _lark_next_message_id:",
            "        on_message_started(",
            "            message_id=_lark_next_message_id,",
            "            chat_id=getattr(next_source, 'chat_id', source.chat_id),",
            "            anchor_id=_lark_next_anchor_id,",
            "        )",
            "except Exception:",
            "    pass",
        ],
    )


def _cron_deliver_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_CRON_DELIVER,
        MK_CRON_DELIVER_END,
        [
            "try:",
            "    if platform_name.lower() in ('feishu', 'lark'):",
            "        if '_hermes_lark_cron_seen' not in locals():",
            "            _hermes_lark_cron_seen = set()",
            "        _hermes_lark_cron_key = (str(chat_id), cleaned_delivery_content.strip())",
            "        if _hermes_lark_cron_key in _hermes_lark_cron_seen:",
            "            delivered = True",
            "            continue",
            "        from hermes_lark_streaming.patch import on_cron_deliver",
            "        if on_cron_deliver(chat_id=chat_id, content=cleaned_delivery_content.strip(), loop=loop):",
            "            _hermes_lark_cron_seen.add(_hermes_lark_cron_key)",
            "            delivered = True",
            "            continue",
            "except Exception:",
            "    pass",
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
            "except Exception:",
            "    pass",
        ],
    )


def _remove_block(content: str, begin: str, end: str) -> str:
    lines = content.splitlines(keepends=True)
    begin_idx = end_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == begin:
            begin_idx = i
        if stripped == end:
            end_idx = i
            break
    if begin_idx is not None and end_idx is not None:
        return "".join(lines[:begin_idx] + lines[end_idx + 1 :])
    return content


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件再 rename，防止崩溃时文件损坏."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, dir=str(path.parent), prefix=".hermes_lark_", mode="w", encoding="utf-8"
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


class Patcher:
    """管理 AST 注入的安装和移除."""

    MARKERS: list[tuple[str, str]] = MARKERS

    def __init__(self, run_path: Path = _RUN_PATH) -> None:
        self.run_path = run_path
        if not run_path.exists():
            raise PatcherError(f"run.py not found: {run_path}")

    def is_patched(self) -> bool:
        return MK_START in self.run_path.read_text(encoding="utf-8")

    def is_fully_patched(self) -> bool:
        content = self.run_path.read_text(encoding="utf-8")
        return all(begin in content and end in content for begin, end in self.MARKERS)

    def verify_target(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        tree = ast.parse(content)

        handler = _find_func_body(tree, content.splitlines(keepends=True), "_handle_message_with_agent")
        if handler is None:
            raise PatcherError("Cannot find _handle_message_with_agent in run.py — Hermes version may be incompatible")

        anchor_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "emit":
                    hooks_obj = func.value
                    if (
                        isinstance(hooks_obj, ast.Attribute)
                        and hooks_obj.attr == "hooks"
                        and (node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "agent:end")
                    ):
                        anchor_found = True
                        break
        if not anchor_found:
            raise PatcherError(
                "Cannot find hooks.emit('agent:end', ...) anchor in run.py — Hermes version may be incompatible"
            )

        required_callbacks = {
            "progress_callback": False,
            "_stream_delta_cb": False,
            "_interim_assistant_cb": False,
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in required_callbacks:
                required_callbacks[node.name] = True
        missing = [name for name, found in required_callbacks.items() if not found]
        if missing:
            raise PatcherError(
                f"Missing injection targets in run.py: {', '.join(missing)} — Hermes version may be incompatible"
            )

        if "Restart typing indicator so the user sees activity" not in content:
            raise PatcherError("Cannot find interrupt anchor in run.py — Hermes version may be incompatible")

        if 'was_interrupted = result.get("interrupted")' not in content:
            raise PatcherError("Cannot find queued follow-up boundary in run.py — Hermes version may be incompatible")

        if "return _preserve_queued_followup_history_offset(result, followup_result)" not in content:
            raise PatcherError("Cannot find queued follow-up return in run.py — Hermes version may be incompatible")

        if "agent.reasoning_config = reasoning_config" not in content:
            raise PatcherError("Cannot find reasoning_config anchor in run.py — Hermes version may be incompatible")

        if "agent.background_review_callback = _bg_review_send" not in content:
            raise PatcherError(
                "Cannot find background_review_callback anchor in run.py — Hermes version may be incompatible"
            )

        if "images, text_content = adapter.extract_images(response)" not in content:
            raise PatcherError(
                "Cannot find background deliver anchor in run.py — Hermes version may be incompatible"
            )

        normalize_site = _find_handle_message_source_site(tree, content.splitlines(keepends=True))
        if normalize_site is None:
            raise PatcherError(
                "Cannot find _handle_message source anchor in run.py — Hermes version may be incompatible"
            )

    def apply(self) -> None:
        if self.is_fully_patched():
            return

        self.verify_target()
        content = self.run_path.read_text(encoding="utf-8")
        if self.is_patched():
            for begin, end in self.MARKERS:
                content = _remove_block(content, begin, end)
        else:
            self._backup()
        content = self._inject_all(content)
        _atomic_write(self.run_path, content)

    def remove(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        if not any(begin in content for begin, _ in self.MARKERS):
            return
        for begin, end in self.MARKERS:
            content = _remove_block(content, begin, end)
        _atomic_write(self.run_path, content)

    def restore(self) -> None:
        backup = self.run_path.with_suffix(self.run_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            raise PatcherError(f"No backup found: {backup}")
        shutil.copy2(backup, self.run_path)

    def _backup(self) -> None:
        backup = self.run_path.with_suffix(self.run_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(self.run_path, backup)

    def _inject_all(self, content: str) -> str:
        tree = ast.parse(content)
        lines = content.splitlines(keepends=True)

        hook_defs: list[tuple[str, str, tuple[int, str] | None]] = [
            ("normalize", "normalize", _find_handle_message_source_site(tree, lines)),
            ("start", "start", _find_func_body(tree, lines, "_handle_message_with_agent")),
            ("complete", "complete", _find_handler_return(tree, lines)),
            ("followup_complete", "followup_complete", _find_followup_complete_site(tree, lines)),
            ("followup_result", "followup_result", _find_followup_result_site(tree, lines)),
            ("abort", "abort", _find_handler_abort(tree, lines)),
            ("interrupt", "interrupt", _find_interrupt_site(tree, lines)),
            ("tool", "tool", _find_func_body(tree, lines, "progress_callback")),
            ("answer", "answer", _find_func_body(tree, lines, "_stream_delta_cb")),
            ("thinking", "thinking", _find_func_body(tree, lines, "_interim_assistant_cb")),
            ("reasoning", "reasoning", _find_reasoning_site(tree, lines)),
            ("background_review", "background_review", _find_background_review_site(tree, lines)),
            ("bg_deliver", "bg_deliver", _find_bg_deliver_site(tree, lines)),
        ]

        sites: list[tuple[int, str, str]] = []
        for hook_fn_name, name, loc in hook_defs:
            if loc is None:
                _logger.warning("Patcher: cannot find %s — hook skipped", name)
            else:
                sites.append((loc[0], loc[1], hook_fn_name))

        sites.sort(key=lambda x: x[0], reverse=True)
        _HOOK_FNS = {
            "normalize": _feishu_normalize_hook,
            "start": _start_hook,
            "complete": _complete_hook,
            "followup_complete": _followup_complete_hook,
            "followup_result": _followup_result_hook,
            "abort": _abort_hook,
            "interrupt": _interrupt_hook,
            "tool": _tool_hook,
            "answer": _answer_hook,
            "thinking": _thinking_hook,
            "reasoning": _reasoning_hook,
            "background_review": _background_review_hook,
            "bg_deliver": _bg_deliver_hook,
        }
        for idx, indent, fn_name in sites:
            hook = _HOOK_FNS[fn_name](indent)
            lines[idx:idx] = hook.splitlines(keepends=True)

        return "".join(lines)


def _find_func_body(tree: ast.Module, lines: list[str], name: str) -> tuple[int, str] | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            body = node.body
            start = 0
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                start = 1
            if start < len(body):
                lineno = body[start].lineno - 1
                indent = _safe_indent(lines, lineno)
                return lineno, indent
    return None


def _find_handle_message_source_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_message":
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "source"
                    and isinstance(stmt.value, ast.Attribute)
                    and stmt.value.attr == "source"
                    and isinstance(stmt.value.value, ast.Name)
                    and stmt.value.value.id == "event"
                ):
                    lineno = stmt.end_lineno or stmt.lineno
                    return lineno, _safe_indent(lines, stmt.lineno - 1)
    return None


def _find_handler_return(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("_already_sent = bool("):
            indent = _safe_indent(lines, i)
            return i, indent

    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_handle_message_with_agent":
            returns = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Return)
                and isinstance(n.value, ast.Name)
                and n.value.id == "response"
                and n.lineno is not None
            ]
            if returns:
                target = max(returns, key=lambda x: x.lineno)
                lineno = target.lineno - 1
                indent = _safe_indent(lines, lineno)
                return lineno, indent
    return None


def _find_handler_abort(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if "Discarding stale agent result" in line:
            for j in range(i + 1, min(i + 20, len(lines))):
                if lines[j].strip() == "return None":
                    indent = _safe_indent(lines, j)
                    return j, indent
            break
    return None


def _find_interrupt_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if "Restart typing indicator so the user sees activity" in line:
            indent = _safe_indent(lines, i)
            return i, indent
    return None


def _find_followup_complete_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if line.strip() == 'was_interrupted = result.get("interrupted")':
            return i, _safe_indent(lines, i)
    return None


def _find_followup_result_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if line.strip() == "return _preserve_queued_followup_history_offset(result, followup_result)":
            return i, _safe_indent(lines, i)
    return None


def _find_reasoning_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if line.strip() == "agent.reasoning_config = reasoning_config":
            return i + 1, _safe_indent(lines, i)
    return None


def _find_background_review_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if line.strip() == "agent.background_review_callback = _bg_review_send":
            return i + 1, _safe_indent(lines, i)
    return None


def _find_bg_deliver_site(tree: ast.Module, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if line.strip() == "images, text_content = adapter.extract_images(response)":
            return i + 1, _safe_indent(lines, i)
    return None


def _safe_indent(lines: list[str], lineno: int) -> str:
    """获取缩进，跳过空行."""
    for i in range(lineno, -1, -1):
        if 0 <= i < len(lines) and lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    for i in range(lineno + 1, len(lines)):
        if lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    return ""


class CronPatcher:
    """注入 CRON_DELIVER hook 到 cron/scheduler.py 的 _deliver_result."""

    def __init__(self, cron_path: Path = _CRON_PATH) -> None:
        self.cron_path = cron_path
        if not cron_path.exists():
            raise PatcherError(f"scheduler.py not found: {cron_path}")

    def is_patched(self) -> bool:
        return MK_CRON_DELIVER in self.cron_path.read_text(encoding="utf-8")

    def verify_target(self) -> None:
        content = self.cron_path.read_text(encoding="utf-8")
        if "delivered = False" not in content:
            raise PatcherError("Cannot find 'delivered = False' anchor in scheduler.py")
        if "cleaned_delivery_content" not in content:
            raise PatcherError("Cannot find 'cleaned_delivery_content' in scheduler.py")

    def apply(self) -> None:
        if self.is_patched():
            return
        self.verify_target()
        self._backup()
        lines = self.cron_path.read_text(encoding="utf-8").splitlines(keepends=True)

        inject_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "delivered = False":
                inject_idx = i
                break
        if inject_idx is None:
            raise PatcherError("Cannot find 'delivered = False' anchor")

        indent = _safe_indent(lines, inject_idx)
        hook = _cron_deliver_hook(indent)
        lines[inject_idx + 1 : inject_idx + 1] = hook.splitlines(keepends=True)
        _atomic_write(self.cron_path, "".join(lines))

    def remove(self) -> None:
        content = self.cron_path.read_text(encoding="utf-8")
        if MK_CRON_DELIVER not in content:
            return
        content = _remove_block(content, MK_CRON_DELIVER, MK_CRON_DELIVER_END)
        _atomic_write(self.cron_path, content)

    def restore(self) -> None:
        backup = self.cron_path.with_suffix(self.cron_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            raise PatcherError(f"No backup found: {backup}")
        shutil.copy2(backup, self.cron_path)

    def _backup(self) -> None:
        backup = self.cron_path.with_suffix(self.cron_path.suffix + _BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(self.cron_path, backup)
