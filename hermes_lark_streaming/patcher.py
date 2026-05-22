"""AST Patcher — 在 Hermes gateway/run.py 中注入 Hook 调用."""

from __future__ import annotations

import ast
import logging
import shutil
from pathlib import Path

_logger = logging.getLogger("hermes_lark_streaming")


PREFIX = "HERMES_LARK"

_HOOK_NAMES = [
    "START",
    "COMPLETE",
    "TOOL",
    "ANSWER",
    "THINKING",
    "REASONING",
    "BACKGROUND_REVIEW",
    "ABORT",
    "INTERRUPT",
]
MARKERS: list[tuple[str, str]] = [(f"# {PREFIX}_{n}_BEGIN", f"# {PREFIX}_{n}_END") for n in _HOOK_NAMES]

MK_START, MK_START_END = MARKERS[0]
MK_COMPLETE, MK_COMPLETE_END = MARKERS[1]
MK_TOOL, MK_TOOL_END = MARKERS[2]
MK_ANSWER, MK_ANSWER_END = MARKERS[3]
MK_THINKING, MK_THINKING_END = MARKERS[4]
MK_REASONING, MK_REASONING_END = MARKERS[5]
MK_BACKGROUND_REVIEW, MK_BACKGROUND_REVIEW_END = MARKERS[6]
MK_ABORT, MK_ABORT_END = MARKERS[7]
MK_INTERRUPT, MK_INTERRUPT_END = MARKERS[8]

_BACKUP_SUFFIX = ".hermes_lark.bak"

_RUN_PATH = Path.home() / ".hermes" / "hermes-agent" / "gateway" / "run.py"


def _make_hook(indent: str, begin: str, end: str, body_lines: list[str]) -> str:
    return f"{indent}{begin}\n" + "".join(f"{indent}{line}\n" for line in body_lines) + f"{indent}{end}\n"


def _start_hook(indent: str) -> str:
    return _make_hook(
        indent,
        MK_START,
        MK_START_END,
        [
            "try:",
            "    from hermes_lark_streaming.patch import on_message_started",
            "    _lark_message_id = self._reply_anchor_for_event(event) or event.message_id",
            "    on_message_started(message_id=_lark_message_id, chat_id=source.chat_id)",
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
            "    from hermes_lark_streaming.patch import on_message_completed",
            "    _lark_message_id = self._reply_anchor_for_event(event) or event.message_id",
            "    _lark_card_sent = on_message_completed(",
            "        message_id=_lark_message_id,",
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
            "    from hermes_lark_streaming.patch import on_message_interrupted",
            "    if was_interrupted and next_message_id:",
            "        on_message_interrupted(",
            "            message_id=event_message_id,",
            "            new_message_id=next_message_id,",
            "            chat_id=source.chat_id,",
            "        )",
            "except Exception:",
            "    pass",
        ],
    )


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

        if "agent.reasoning_config = reasoning_config" not in content:
            raise PatcherError("Cannot find reasoning_config anchor in run.py — Hermes version may be incompatible")

        if "agent.background_review_callback = _bg_review_send" not in content:
            raise PatcherError(
                "Cannot find background_review_callback anchor in run.py — Hermes version may be incompatible"
            )

    def apply(self) -> None:
        if self.is_fully_patched():
            return

        self.verify_target()
        content = self.run_path.read_text(encoding="utf-8")
        if self.is_patched():
            for begin, end in self.MARKERS:
                content = self._remove_block(content, begin, end)
        else:
            self._backup()
        content = self._inject_all(content)
        self.run_path.write_text(content, encoding="utf-8")

    def remove(self) -> None:
        content = self.run_path.read_text(encoding="utf-8")
        if not any(begin in content for begin, _ in self.MARKERS):
            return
        for begin, end in self.MARKERS:
            content = self._remove_block(content, begin, end)
        self.run_path.write_text(content, encoding="utf-8")

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
            ("start", "start", _find_func_body(tree, lines, "_handle_message_with_agent")),
            ("complete", "complete", _find_handler_return(tree, lines)),
            ("abort", "abort", _find_handler_abort(tree, lines)),
            ("interrupt", "interrupt", _find_interrupt_site(tree, lines)),
            ("tool", "tool", _find_func_body(tree, lines, "progress_callback")),
            ("answer", "answer", _find_func_body(tree, lines, "_stream_delta_cb")),
            ("thinking", "thinking", _find_func_body(tree, lines, "_interim_assistant_cb")),
            ("reasoning", "reasoning", _find_reasoning_site(tree, lines)),
            ("background_review", "background_review", _find_background_review_site(tree, lines)),
        ]

        sites: list[tuple[int, str, str]] = []
        for hook_fn_name, name, loc in hook_defs:
            if loc is None:
                _logger.warning("Patcher: cannot find %s — hook skipped", name)
            else:
                sites.append((loc[0], loc[1], hook_fn_name))

        sites.sort(key=lambda x: x[0], reverse=True)
        _HOOK_FNS = {
            "start": _start_hook,
            "complete": _complete_hook,
            "abort": _abort_hook,
            "interrupt": _interrupt_hook,
            "tool": _tool_hook,
            "answer": _answer_hook,
            "thinking": _thinking_hook,
            "reasoning": _reasoning_hook,
            "background_review": _background_review_hook,
        }
        for idx, indent, fn_name in sites:
            hook = _HOOK_FNS[fn_name](indent)
            lines[idx:idx] = hook.splitlines(keepends=True)

        return "".join(lines)

    def _remove_block(self, content: str, begin: str, end: str) -> str:
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


def _safe_indent(lines: list[str], lineno: int) -> str:
    """获取缩进，跳过空行."""
    for i in range(lineno, -1, -1):
        if 0 <= i < len(lines) and lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    for i in range(lineno + 1, len(lines)):
        if lines[i].strip():
            return lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    return ""
