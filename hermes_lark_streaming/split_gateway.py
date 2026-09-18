"""Insertion-only hooks for the split Hermes gateway.

The caller owns marker removal, layout detection, and transactional file writes.
No upstream modules are imported or files modified by this module.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable, Iterator

GATEWAY_FILES = ("run_inbound.py", "run_turn.py", "run_turn_runner.py", "run_busy.py")


def _scoped_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Walk one lexical scope, not nested functions or classes."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _scoped_nodes(child)


def inject_gateway(filename: str, content: str) -> str:
    """Generate and compile one clean split gateway file; reject missing/ambiguous anchors.

    All insertions use existing ``patcher.MARKERS`` (repeated pairs are intentional).
    Pass a basename from GATEWAY_FILES and source with all known markers removed.
    """
    # Local import: patcher can delegate here without an import cycle.
    from . import patcher as p

    if filename not in GATEWAY_FILES:
        raise p.PatcherError(f"Unsupported split gateway file: {filename}")
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as exc:
        raise p.PatcherError(f"Invalid {filename}: {exc}") from exc
    lines = content.splitlines(keepends=True)
    edits: list[tuple[int, str]] = []

    def scope(name: str, parent: ast.AST = tree) -> ast.AST:
        matches = [n for n in ast.walk(parent)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(matches) != 1:
            raise p.PatcherError(f"{filename}: expected one function {name}, found {len(matches)}")
        return matches[0]

    def select(fn: str, predicate: Callable[[ast.AST], bool], count: int = 1) -> list[ast.AST]:
        matches = [n for n in _scoped_nodes(scope(fn)) if predicate(n)]
        if len(matches) != count:
            raise p.PatcherError(f"{filename}:{fn}: expected {count} anchors, found {len(matches)}")
        return matches

    def statement(fn: str, source: str, count: int = 1) -> list[ast.AST]:
        expected = ast.dump(ast.parse(source).body[0])
        return select(fn, lambda n: isinstance(n, ast.stmt) and ast.dump(n) == expected, count)

    def assignment(fn: str, target: str) -> ast.AST:
        return select(fn, lambda n: isinstance(n, ast.Assign) and any(
            ast.unparse(t) == target for t in n.targets))[0]

    def insert(node: ast.AST, hook: str | Callable[[str], str], *, after: bool = False) -> None:
        line = node.end_lineno if after else node.lineno - 1  # type: ignore[attr-defined]
        indent = " " * node.col_offset  # type: ignore[attr-defined]
        edits.append((line, hook(indent) if callable(hook) else textwrap.indent(hook, indent)))

    def block(marker: str, body: str) -> str:
        return p._make_hook("", f"# HERMES_LARK_{marker}_BEGIN", f"# HERMES_LARK_{marker}_END",
                            textwrap.dedent(body).strip().splitlines())

    def guarded(marker: str, body: str) -> str:
        body = textwrap.indent(textwrap.dedent(body).strip(), "    ")
        return block(marker, "try:\n" + body + "\n" + "\n".join(p._hook_exception_lines(marker.lower())))

    identity = "(ctx.inbound_message_id or ctx.event_message_id)"
    turn_identity = "(turn_ctx.inbound_message_id or turn_ctx.event_message_id)"

    def ctx_hook(generator: Callable[[str], str]) -> Callable[[str], str]:
        return lambda indent: generator(indent).replace("ctx.event_message_id", identity)

    if filename == "run_inbound.py":
        for node in statement("_hm_admit_event", "source = event.source", 2):
            insert(node, p._feishu_normalize_hook, after=True)
        # This outer finally also covers preparation failures and cancellation.
        restores = {ast.dump(ast.parse(source).body[0]) for source in (
            "self._restore_moa_one_shot(event, _quick_key)",
            "self._restore_pending_one_turn_model_override(_quick_key, _run_generation)",
        )}
        node = select("_handle_message", lambda n: isinstance(n, ast.Expr) and ast.dump(n) in restores)[0]
        inbound_handler = scope("_handle_message")
        if not isinstance(inbound_handler, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(
            isinstance(n, ast.Try) and node in n.finalbody for n in inbound_handler.body
        ):
            raise p.PatcherError(f"{filename}: one-turn restore must remain in the outer finally")
        insert(node, p._abort_hook)

    elif filename == "run_busy.py":
        node = select("_busy_stop_command", lambda n: isinstance(n, ast.Return))[0]
        insert(node, p._stop_hook)

    elif filename == "run_turn_runner.py":
        # After live status and native log recording, before the progress-queue gate.
        node = select("progress_callback", lambda n: isinstance(n, ast.If)
                      and ast.unparse(n.test) == "not ctx.progress_queue or not ctx._run_still_current()")[0]
        insert(node, guarded("TOOL", f"""
            from hermes_lark_streaming.patch import on_tool_updated
            if ctx._run_still_current() and event_type in ('tool.started', 'tool.completed'):
                if on_tool_updated(message_id={identity}, tool_name=tool_name or '',
                                   status='started' if event_type == 'tool.started' else 'completed',
                                   detail=preview or ''):
                    return
        """))
        setup = "_setup_stream_consumer"
        # Keep native setup intact for disabled/unregistered turns and other platforms.
        select(setup, lambda n: isinstance(n, ast.If) and ast.unparse(n.test) == "delta_sinks")
        stream = scope("stream_delta_cb", scope(setup))
        if not any(isinstance(n, ast.Expr) and ast.unparse(n) == "sink.on_delta(text)"
                   for n in _scoped_nodes(stream)):
            raise p.PatcherError(f"{filename}: missing native delta fan-out")
        node = assignment(setup, "stts")
        insert(node, guarded("ANSWER", f"""
            if ctx.source.platform.value.lower() in ('feishu', 'lark'):
                from hermes_lark_streaming.patch import get_controller, on_answer_delta, on_thinking_delta
                _lark_ctrl = get_controller()
                # Reserve one text transport until the outer completion hook decides delivery.
                # Include failed creation sessions: native finish() must not send ahead of the
                # completion hook clearing already_sent and delivering the full fallback text.
                if _lark_ctrl.enabled and {identity} in _lark_ctrl._sessions:
                    def _lark_stream_delta(text):
                        if not ctx._run_still_current():
                            return
                        try:
                            if text:
                                on_answer_delta(message_id={identity}, text=text)
                        except Exception:
                            logger.debug('Card streaming callback failed', exc_info=True)
                        if stts is not None:
                            try:
                                stts.on_delta(text)
                            except Exception:
                                logger.debug('Card streaming TTS failed', exc_info=True)
                    def _lark_interim_only(text, *, already_streamed=False):
                        if not ctx._run_still_current():
                            return
                        try:
                            if text and not already_streamed:
                                on_thinking_delta(message_id={identity}, text=text)
                        except Exception:
                            logger.debug('Card interim callback failed', exc_info=True)
                        if stts is not None:
                            try:
                                stts.on_delta(None)
                                if not already_streamed:
                                    stts.on_delta(text)
                                    stts.on_delta(None)
                            except Exception:
                                logger.debug('Card interim TTS failed', exc_info=True)
                    return None, _lark_stream_delta, _lark_interim_only, True
        """), after=True)
        wire = "_wire_turn_agent_callbacks"
        node = assignment(wire, "(agent.reasoning_config, agent.service_tier)")
        insert(node, ctx_hook(p._reasoning_hook), after=True)
        node = assignment(wire, "(agent.background_review_callback, bg_release)")
        insert(node, ctx_hook(p._background_review_hook), after=True)
        node = statement(wire, "agent.clarify_callback = self._clarify_callback_sync")[0]
        insert(node, ctx_hook(p._clarify_hook), after=True)

    else:
        handler = "_handle_message_with_agent"
        node = statement(handler, "event._heartbeat_execution_started = True")[0]
        insert(node, p._start_hook, after=True)
        node = statement(handler, "self._hmwa_discard_stale_result(source, _quick_key, run_generation)")[0]
        insert(node, p._abort_hook)
        node = assignment(handler, "_footer_line")
        insert(node, block("COMPLETE", """
            _lark_original_response = response
            _lark_footer_offset = (
                len(response) if _footer_line and response and not agent_result.get('already_sent')
                and not _intentional_silence else None
            )
        """), after=True)
        node = select(handler, lambda n: isinstance(n, ast.Return) and isinstance(n.value, ast.Await)
                      and isinstance(n.value.value, ast.Call)
                      and ast.unparse(n.value.value.func) == "self._hmwa_deliver_turn_response")[0]
        # Complete after transcript persistence and response rewrites (including context-reset
        # notices), but before the native delivery decision. Strip only the footer we observed
        # upstream append; CardKit renders its own footer from the structured usage fields.
        complete = p._complete_hook("").replace("duration=_response_time", "duration=_turn_seconds")
        complete = complete.replace("answer=response", "answer=_lark_completion_answer")
        complete = complete.replace(
            "is_error=bool(agent_result.get('failed')),",
            "is_error=bool(agent_result.get('failed')),\n"
            "        reconcile_answer=bool(agent_result.get('failed') or agent_result.get('response_transformed')\n"
            "                              or _lark_completion_answer != _lark_original_response),",
        )
        # Silence must terminate an existing streaming session without rendering its marker.
        complete = "\n".join(complete.splitlines()[1:-1]) + "\n"
        complete_body = textwrap.indent(complete, "    ")
        prefix = textwrap.dedent("""
            _lark_completion_answer = response
            _lark_footer_suffix = '\\n\\n' + _footer_line
            if (_lark_footer_offset is not None
                    and response[_lark_footer_offset:].startswith(_lark_footer_suffix)):
                _lark_completion_answer = (response[:_lark_footer_offset]
                    + response[_lark_footer_offset + len(_lark_footer_suffix):])
        """)
        suffix = textwrap.dedent("""
            if (_lark_footer_offset is None and _footer_line and response
                    and not agent_result.get('already_sent') and not _intentional_silence):
                response = f'{response}\\n\\n{_footer_line}'
        """)
        insert(node, block("COMPLETE", prefix + "if _intentional_silence:\n" + textwrap.indent(
            p._abort_hook(""), "    ") + "else:\n" + complete_body + suffix))
        node = statement(handler, "self._clear_session_env(_session_env_tokens)")[0]
        insert(node, guarded("ABORT", """
            from hermes_lark_streaming.patch import on_message_aborted
            _lark_result = locals().get('agent_result')
            _lark_cleanup_id = (_lark_result.get('_hermes_lark_completion_id')
                                if isinstance(_lark_result, dict) else None) or event.message_id
            on_message_aborted(message_id=_lark_cleanup_id)
        """))
        node = statement("_run_background_task_inner", "images, text_content = adapter.extract_images(response)")[0]
        insert(node, p._bg_deliver_hook, after=True)

        first = "_run_agent_deliver_first_response"
        node = assignment(first, "_already_streamed")
        # No use of on_queued_followup_boundary: it destroys attachment-bearing text.
        insert(node, guarded("FOLLOWUP_COMPLETE", f"""
            from hermes_lark_streaming.patch import (
                on_message_completed_wait, on_message_needs_text_fallback, on_message_aborted,
            )
            _lark_id = _delivery_result.get('_hermes_lark_completion_id') or {turn_identity}
            if self._is_intentional_silence(_delivery_result, first_response):
                on_message_aborted(message_id=_lark_id)
            elif not _delivery_result.get('interrupted'):
                _lark_sent = await on_message_completed_wait(
                    message_id=_lark_id, answer=first_response,
                    is_error=bool(_delivery_result.get('failed')), duration=0.0,
                    reconcile_answer=bool(_delivery_result.get('failed')
                                          or _delivery_result.get('response_transformed')),
                    model=_delivery_result.get('model', ''),
                    tokens={{'input_tokens': _delivery_result.get('input_tokens', 0),
                            'output_tokens': _delivery_result.get('output_tokens', 0)}},
                    context={{'used_tokens': _delivery_result.get('last_prompt_tokens', 0),
                             'max_tokens': _delivery_result.get('context_length', 0)}},
                )
                if _lark_sent:
                    _already_streamed = True
                    for _lark_result in (_delivery_result, result):
                        if isinstance(_lark_result, dict):
                            _lark_result['response_previewed'] = True
                            _lark_result['already_sent'] = True
                elif on_message_needs_text_fallback(message_id=_lark_id):
                    _already_streamed = False
                    for _lark_result in (_delivery_result, result):
                        if isinstance(_lark_result, dict):
                            _lark_result.pop('already_sent', None)
                            _lark_result.pop('response_previewed', None)
        """), after=True)
        follow = "_run_agent_queued_followup"
        node = assignment(follow, "followup_result")
        insert(node, guarded("INTERRUPT", f"""
            if source.platform.value.lower() in ('feishu', 'lark'):
                from hermes_lark_streaming.patch import on_message_interrupted, on_message_started, on_message_aborted
                _lark_next_id = getattr(pending_event, 'message_id', None) or next_message_id
                turn_ctx._hermes_lark_followup_id = _lark_next_id
                if result.get('interrupted') and _lark_next_id:
                    on_message_interrupted(message_id={turn_identity}, new_message_id=_lark_next_id,
                                           chat_id=next_source.chat_id, anchor_id=next_message_id,
                                           session_key=next_session_key)
                elif result.get('interrupted'):
                    on_message_aborted(message_id={turn_identity})
                elif _lark_next_id:
                    on_message_started(message_id=_lark_next_id, chat_id=next_source.chat_id,
                                       anchor_id=next_message_id, session_key=next_session_key)
        """))
        # Older split gateways omit the inbound identity; newer ones already carry it.
        value = node.value if isinstance(node, ast.Assign) else None
        call = value.value if isinstance(value, ast.Await) else None
        if (not isinstance(call, ast.Call) or ast.unparse(call.func) != "self._run_agent"
                or any(k.arg is None for k in call.keywords)):
            raise p.PatcherError(f"{filename}:{follow}: unexpected recursive call")
        inbound = next((k for k in call.keywords if k.arg == "inbound_message_id"), None)
        if inbound is None:
            keyword = next((k for k in call.keywords if k.arg == "message_type"), None)
            if keyword is None:
                raise p.PatcherError(f"{filename}:{follow}: missing message_type keyword")
            insert(keyword, block("INTERRUPT", "inbound_message_id=getattr(pending_event, 'message_id', None),"))
        else:
            if ast.unparse(inbound.value) != "next_inbound_id":
                raise p.PatcherError(f"{filename}:{follow}: unsupported inbound_message_id source")
            initial = statement(follow, "next_inbound_id = None")[0]
            resolved = statement(follow,
                                 "next_inbound_id = str(pending_event.message_id) "
                                 "if getattr(pending_event, 'message_id', None) else None")[0]
            select(follow, lambda n: isinstance(n, ast.Name) and n.id == "next_inbound_id"
                   and isinstance(n.ctx, (ast.Store, ast.Del)), count=2)
            guard = select(follow, lambda n: isinstance(n, ast.If)
                           and ast.unparse(n.test) == "pending_event is not None")[0]
            fn = scope(follow)
            if (not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or not isinstance(initial, ast.Assign) or not isinstance(guard, ast.If)
                    or initial not in fn.body
                    or resolved not in guard.body or not initial.lineno < guard.lineno < call.lineno):
                raise p.PatcherError(f"{filename}:{follow}: unexpected inbound identity resolution")
        insert(node, guarded("FOLLOWUP_RESULT", """
            from hermes_lark_streaming.patch import on_queued_followup_result
            _lark_next_id = getattr(pending_event, 'message_id', None) or next_message_id
            if _lark_next_id:
                on_queued_followup_result(message_id=_lark_next_id, followup_result=followup_result)
        """), after=True)
        node = assignment("_run_agent_cleanup_turn_tasks", "(stream_consumer_holder, session_key)")
        # The parent context owns the newly started follow-up until its recursive call
        # returns. This also catches failures before the child's own cleanup is armed.
        insert(node, guarded("ABORT", f"""
            import sys as _lark_sys
            if _lark_sys.exc_info()[0] is not None:
                from hermes_lark_streaming.patch import on_message_aborted
                _lark_child_id = getattr(turn_ctx, '_hermes_lark_followup_id', None)
                if _lark_child_id:
                    on_message_aborted(message_id=_lark_child_id)
                on_message_aborted(message_id={turn_identity})
        """))

    for line, addition in sorted(edits, key=lambda edit: edit[0], reverse=True):
        if "\r\n" in content:
            addition = addition.replace("\n", "\r\n")
        lines.insert(line, addition)
    generated = "".join(lines)
    try:
        compile(generated, filename, "exec")
    except (SyntaxError, ValueError) as exc:
        raise p.PatcherError(f"Generated {filename} does not compile: {exc}") from exc
    return generated
