"""Execute patched methods from the pinned upstream, without importing or installing Hermes."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import queue
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime
from functools import cache
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_sources import patched_gateway, source_at

from hermes_lark_streaming.patcher import MARKERS, PatcherError, _remove_block
from hermes_lark_streaming.split_gateway import GATEWAY_FILES, inject_gateway


def _stub_gateway_packages(monkeypatch):
    """Register bare parent packages so upstream late imports resolve without executing Hermes.

    ``gateway/__init__.py`` pulls in every adapter, so a name-only sys.modules stub is
    the only way to execute a single split module in isolation (Hermes >= 0.21.1).
    """
    modules = {
        "gateway": dict(__path__=[]),
        "gateway.platforms": dict(__path__=[]),
        "gateway.platforms.base": dict(
            BasePlatformAdapter=type("BasePlatformAdapter", (), {}),
            MessageEvent=type("MessageEvent", (), {}),
            merge_pending_message_event=Mock(), EphemeralReply=str,
        ),
        "gateway.run": dict(_preserve_queued_followup_history_offset=lambda old, new: new),
    }
    for name, attrs in modules.items():
        loaded = ModuleType(name)
        loaded.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, loaded)


def _event(message_id, anchor, *, present=True):
    """Minimal event with distinct inbound identity and reply anchor."""
    fields = dict(anchor=anchor)
    if present:
        fields["message_id"] = message_id
    return NS(**fields)


@pytest.fixture(scope="module")
def sources():
    return {name: source_at(f"gateway/{name}") for name in GATEWAY_FILES}


@pytest.fixture(scope="module")
def patched(sources):
    return {name: patched_gateway(name) for name in sources}


@cache
def _method_code(content, filename, name):
    tree = ast.parse(content)
    matches = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(matches) == 1
    node = matches[0]
    node.decorator_list = []
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.Module(body=[future, node], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, filename, "exec")


def method(patched, filename, name, **extra):
    namespace = dict(logger=logging.getLogger(__name__), time=time, suppress=suppress,
                     inspect=inspect, threading=threading, datetime=datetime,
                     **extra)
    exec(_method_code(patched[filename], filename, name), namespace)
    return namespace[name]


@pytest.fixture
def hooks(monkeypatch):
    hooks = NS(**{name: Mock(return_value=False) for name in (
        "on_feishu_normalize", "on_message_started", "on_message_aborted", "on_message_interrupted",
        "on_message_needs_text_fallback", "on_tool_updated", "on_answer_delta", "on_thinking_delta",
        "on_reasoning_delta", "on_background_review_message", "on_clarify_enter", "on_clarify_exit",
    )})
    hooks.on_message_completed_wait = AsyncMock(return_value=False)
    hooks.on_session_aborted = AsyncMock(return_value=False)
    hooks.on_background_deliver = AsyncMock(return_value=False)
    hooks.on_queued_followup_result = Mock(side_effect=lambda **kw: kw["followup_result"].setdefault(
        "_hermes_lark_completion_id", kw["message_id"]))
    hooks.get_controller = Mock(return_value=NS(
        enabled=True, _sessions={key: object() for key in ("inbound", "first", "second")},
    ))
    monkeypatch.setitem(sys.modules, "hermes_lark_streaming.patch", hooks)
    return hooks


def context(platform="feishu", **overrides):
    data = dict(source=NS(platform=NS(value=platform), chat_id="chat"),
                inbound_message_id="inbound", event_message_id="quoted-anchor", session_key="session",
                _run_still_current=Mock(return_value=True), streaming_tts_consumer_holder=[None],
                stream_consumer_holder=[None], user_config={}, resolve_display_setting=lambda *a: False,
                interim_assistant_messages_enabled=False, _status_adapter=None, _status_thread_metadata={},
                progress_queue=None, log_queue=None, _status_chat_id="chat", run_generation=1)
    data.update(overrides)
    return NS(**data)


def test_round_trip_and_compile_every_file(sources, patched):
    for name, generated in patched.items():
        compile(generated, name, "exec")
        restored = generated
        for begin, end in MARKERS:
            restored = _remove_block(restored, begin, end)
        assert restored == sources[name]
        assert inject_gateway(name, restored) == generated


@pytest.mark.parametrize("filename", GATEWAY_FILES)
def test_missing_scopes_fail_closed(filename):
    with pytest.raises(PatcherError):
        inject_gateway(filename, "def unrelated():\n    return None\n")


def test_invalid_input_and_ambiguous_scope_fail_closed(sources):
    with pytest.raises(PatcherError):
        inject_gateway("run.py", "")
    with pytest.raises(PatcherError):
        inject_gateway("run_busy.py", "invalid python !!!")
    with pytest.raises(PatcherError):
        inject_gateway("run_busy.py", sources["run_busy.py"] + "\nasync def _busy_stop_command():\n    pass\n")
    with pytest.raises(PatcherError):
        inject_gateway("run_turn_runner.py", sources["run_turn_runner.py"].replace("if delta_sinks:", "if True:"))


@pytest.mark.parametrize("owned", [False, True, "error"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("tts", [False, True])
def test_card_turn_reserves_text_transport_and_preserves_tts(patched, hooks, monkeypatch, owned, native, tts):
    stream, voice = Mock(), Mock()
    ctx = context(streaming_tts_consumer_holder=[voice if tts else None],
                  resolve_display_setting=lambda *a: native)
    runner = NS(config=NS(streaming=NS(enabled=True, transport="auto")),
                _adapter_for_source=lambda _: object(),
                _build_stream_consumer_config=lambda *a, **k: ({}, None))
    monkeypatch.setitem(sys.modules, "gateway.stream_consumer", NS(GatewayStreamConsumer=Mock(return_value=stream)))
    owner = NS(_ctx=ctx, _runner=runner)
    if owned == "error":
        hooks.on_answer_delta.side_effect = RuntimeError("plugin unavailable")
    else:
        hooks.on_answer_delta.return_value = owned
    setup = method(patched, "run_turn_runner.py", "_setup_stream_consumer")
    consumer, delta, interim, want_interim = setup(owner, "feishu")
    assert consumer is None
    assert ctx.stream_consumer_holder == [None]
    assert callable(delta)  # also when BOTH native consumers are off
    delta("text")
    hooks.on_answer_delta.assert_called_once_with(message_id="inbound", text="text")
    stream.on_delta.assert_not_called()
    assert voice.on_delta.call_count == int(tts)
    assert want_interim is True
    ctx._run_still_current.return_value = False
    delta("stale")
    assert hooks.on_answer_delta.call_count == 1
    assert voice.on_delta.call_count == int(tts)
    interim("stale")
    hooks.on_thinking_delta.assert_not_called()


def test_other_platform_retains_none_delta(patched, hooks):
    ctx = context(platform="telegram")
    owner = NS(_ctx=ctx, _runner=NS(config=NS(streaming=object())))
    _, delta, _, _ = method(patched, "run_turn_runner.py", "_setup_stream_consumer")(owner, "telegram")
    assert delta is None
    hooks.on_answer_delta.assert_not_called()


@pytest.mark.parametrize("owned", [True, False])
def test_tool_logs_exactly_once_and_keeps_native_fallback(patched, hooks, owned):
    hooks.on_tool_updated.return_value = owned
    ctx = context(log_queue=queue.Queue(), progress_queue=queue.Queue(), long_tool_hint_fired=[True],
                  _native_slack_task_cards=False, tool_progress_enabled=True, progress_mode="all", last_tool=[None])
    owner = NS(_ctx=ctx, _progress_live_status=Mock(), _agent_interrupted=lambda: False,
               _progress_build_message=Mock(return_value="native"), _progress_emit=Mock())
    progress = method(patched, "run_turn_runner.py", "progress_callback")
    progress(owner, "tool.started", "terminal", "preview")
    assert ctx.log_queue.qsize() == 1
    assert 'terminal: "preview"' in ctx.log_queue.get()
    assert owner._progress_emit.call_count == int(not owned)
    hooks.on_tool_updated.assert_called_once_with(message_id="inbound", tool_name="terminal",
                                                  status="started", detail="preview")
    ctx.progress_queue = None
    progress(owner, "tool.completed", "terminal")
    assert hooks.on_tool_updated.call_count == 2


def wire_owner(patched, ctx):
    ctx.__dict__.update(progress_callback=None, _voice_ack_guild=[None], _native_slack_task_cards=False,
                        native_tool_complete_callback=None, _hooks_ref=NS(loaded_hooks=False),
                        _status_callback_sync=None, _event_callback_sync=None, _thinking_enabled=False,
                        agent_holder=[None], tools_holder=[None], process_task_id="task", process_baseline=set())
    owner = NS(_ctx=ctx, _runner=NS(_service_tier="normal", _consume_pending_turn_sidecar_notes=lambda _: []),
               _notice_callback_sync=None, _merge_turn_request_overrides=Mock(),
               _clarify_callback_sync=Mock(return_value="choice"), _attach_session_title_callback=Mock(),
               _status_live=lambda: True, _send_status_text=Mock())
    make_bg = method(patched, "run_turn_runner.py", "_make_bg_review_callbacks")
    owner._make_bg_review_callbacks = lambda: make_bg(owner)
    return owner


def test_cached_callback_rebinding_clarify_and_review_release(patched, hooks, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_interim_metadata=lambda m: m,
                                                       _non_conversational_metadata=lambda m, **k: m))
    wire = method(patched, "run_turn_runner.py", "_wire_turn_agent_callbacks")
    adapter = NS(_post_delivery_callbacks={})
    agent = NS()
    owners = []
    for message in ("first", "second"):
        ctx = context(inbound_message_id=message, _status_adapter=adapter)
        owner = wire_owner(patched, ctx)
        owners.append(owner)
        _, delta, interim, want_interim = method(patched, "run_turn_runner.py", "_setup_stream_consumer")(
            owner, "feishu",
        )
        wire(owner, agent, {}, {"enabled": True}, delta, interim, want_interim)
        agent.reasoning_callback("reason")
        agent.interim_assistant_callback("thought")
        agent.interim_assistant_callback("already sent", already_streamed=True)
        assert agent.clarify_callback("question", ["choice"]) == "choice"
    assert [c.kwargs["message_id"] for c in hooks.on_reasoning_delta.call_args_list] == ["first", "second"]
    assert [c.kwargs["message_id"] for c in hooks.on_thinking_delta.call_args_list] == ["first", "second"]
    assert [c.kwargs["message_id"] for c in hooks.on_clarify_enter.call_args_list] == ["first", "second"]
    assert hooks.on_clarify_exit.call_count == 2
    owners[-1]._clarify_callback_sync.side_effect = ValueError("clarify cancelled")
    with pytest.raises(ValueError):
        agent.clarify_callback("question", [])
    assert hooks.on_clarify_exit.call_count == 3
    hooks.on_background_review_message.return_value = True
    agent.background_review_callback("deferred")
    sender = hooks.on_background_review_message.call_args.kwargs["sender"]
    sender("deferred")
    owners[-1]._send_status_text.assert_not_called()
    adapter._post_delivery_callbacks["session"]()
    owners[-1]._send_status_text.assert_called_once()
    hooks.on_background_review_message.return_value = False
    agent.background_review_callback("after release")
    assert owners[-1]._send_status_text.call_count == 2
    owners[0]._send_status_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned,failed,silent", [(True, False, False), (False, False, False),
                                                 (True, True, False), (True, False, True)])
async def test_queued_completion_preserves_media(patched, hooks, owned, failed, silent):
    hooks.on_message_completed_wait.return_value = owned
    hooks.on_message_needs_text_fallback.return_value = not owned
    response = {"final_response": "[SILENT]" if silent else "answer MEDIA:/tmp/file.png", "failed": failed}
    raw = {"final_response": "unshaped original"}
    release = Mock()
    owner = NS(_run_agent_stream_confirmed_final_delivery=Mock(return_value=not owned),
               _is_intentional_silence=lambda r, t: t == "[SILENT]",
               _deliver_queued_first_response=AsyncMock(return_value=True),
               _pop_post_delivery_callback=lambda *a: release)
    fn = method(patched, "run_turn.py", "_run_agent_deliver_first_response")
    await fn(owner, context(), object(), response, raw, None)
    if silent:
        hooks.on_message_completed_wait.assert_not_awaited()
        hooks.on_message_aborted.assert_called_once_with(message_id="inbound")
        owner._deliver_queued_first_response.assert_not_awaited()
    else:
        call = owner._deliver_queued_first_response.call_args
        assert call.args == ("answer MEDIA:/tmp/file.png",)
        assert call.kwargs["text_already_delivered"] is owned
        assert call.kwargs["deliver_media"] is not failed
        assert hooks.on_message_completed_wait.call_args.kwargs["message_id"] == "inbound"
        assert response.get("already_sent", False) is owned
        assert raw.get("already_sent", False) is owned
    assert raw["final_response"] == "unshaped original"
    release.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_multihop_followup_identity_and_anchor(patched, hooks, monkeypatch, interrupted):
    _stub_gateway_packages(monkeypatch)
    fn = method(patched, "run_turn.py", "_run_agent_queued_followup")
    calls = []
    owner = NS(_MAX_INTERRUPT_DEPTH=10, _is_goal_continuation_event=lambda _: False,
               _session_key_for_source=lambda _: "session", _reply_anchor_for_event=lambda e: e.anchor,
               _prepare_profile_scoped_inbound_message_text=AsyncMock(return_value="next"),
               _adapter_for_source=lambda _: None,
               _refresh_agent_cache_message_count=AsyncMock(),
               _run_agent_deliver_first_response=AsyncMock())

    async def run_agent(**kwargs):
        calls.append(kwargs)
        if kwargs["inbound_message_id"] == "B":
            ctx = context(inbound_message_id="B", event_message_id="quote-B", session_id="sid", history=[],
                          _interrupt_depth=1, context_prompt="prompt")
            return await fn(owner, ctx, None, "C", _event("C", "quote-C"), {},
                            {"messages": [], "interrupted": interrupted}, None)
        return {"final_response": "C answer"}

    owner._run_agent = run_agent
    ctx = context(inbound_message_id="A", event_message_id="quote-A", session_id="sid", history=[],
                  _interrupt_depth=0, context_prompt="prompt")
    result = await fn(owner, ctx, None, "B", _event("B", "quote-B"), {},
                      {"messages": [], "interrupted": interrupted}, None)
    assert result["_hermes_lark_completion_id"] == "C"
    assert [(c["inbound_message_id"], c["event_message_id"]) for c in calls] == [("B", "quote-B"), ("C", "quote-C")]
    starts = hooks.on_message_interrupted if interrupted else hooks.on_message_started
    assert starts.call_count == 2
    assert [c.kwargs["anchor_id"] for c in starts.call_args_list] == ["quote-B", "quote-C"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owned,failed,silent", [(True, False, False), (False, False, False),
                                                 (True, True, False), (False, True, False), (False, False, True)])
@pytest.mark.parametrize("path", ["normal", "reset", "preparation", "gate", "stale", "error", "cancel"])
@pytest.mark.parametrize("already_sent", [False, True])
async def test_completion_footer_failure_silence_and_cleanup(
    patched, hooks, monkeypatch, owned, failed, silent, path, already_sent,
):
    monkeypatch.setitem(sys.modules, "gateway.run_heartbeat_acceptance",
                        NS(heartbeat_owner_is_current=lambda *a: path != "gate"))
    hooks.on_message_completed_wait.return_value = owned
    hooks.on_message_needs_text_fallback.return_value = not owned
    event = NS(message_id="inbound", text="question", channel_prompt=None, message_type="text")
    source = NS(platform=NS(value="feishu"), user_name="user", chat_id="chat", thread_id=None, user_id="user")
    entry = NS(session_id="sid")
    prepared = NS(history=[], message_text="question", context_prompt="prompt", persist_user_message="question",
                  persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner="owner")
    result = {"failed": failed, "already_sent": already_sent, "_hermes_lark_completion_id": "deepest",
              "input_tokens": 11, "model": "test-model"}
    owner = NS(_PreparedTurn=NS, _hmwa_resolve_session=AsyncMock(return_value=(source, entry, "session")),
               _hmwa_prepare_turn=AsyncMock(return_value=(prepared, [])), hooks=NS(emit=AsyncMock()),
               _reply_anchor_for_event=lambda _: "quoted-anchor", _run_agent=AsyncMock(return_value=result),
               _hmwa_stop_typing_for_turn=AsyncMock(), _is_session_run_current=lambda *a: True,
               _hmwa_shape_agent_response=AsyncMock(return_value=("[SILENT]" if silent else "answer", silent, [])),
               _hmwa_prepend_reasoning=lambda r, text, *a: text, _hmwa_runtime_footer_line=lambda *a: "FOOTER",
               _hmwa_post_turn_hooks=AsyncMock(), _hmwa_classify_turn_failure=lambda *a: (False, False, False),
               _hmwa_compression_exhaustion_reset=AsyncMock(side_effect=lambda r, text, e, *a: (text, e)),
               _hmwa_persist_turn_transcript=AsyncMock(), _hmwa_agent_error_reply=AsyncMock(return_value="error"),
               _clear_session_env=Mock(), _adapter_for_source=lambda _: None,
               _should_send_voice_reply=lambda *a, **k: False)
    deliver = method(patched, "run_turn.py", "_hmwa_deliver_turn_response")
    owner._hmwa_deliver_turn_response = lambda *a: deliver(owner, *a)
    fn = method(patched, "run_turn.py", "_handle_message_with_agent")
    if path == "reset":
        owner._hmwa_compression_exhaustion_reset.side_effect = lambda r, text, e, *a: (text + "\nreset", e)
    if path == "preparation":
        owner._hmwa_prepare_turn.return_value = ("not prepared", [])
        assert await fn(owner, event, source, "session", 1) == "not prepared"
        hooks.on_message_started.assert_not_called()
        hooks.on_message_completed_wait.assert_not_called()
        return
    if path == "stale":
        owner._is_session_run_current = lambda *a: False
        owner._hmwa_discard_stale_result = Mock()
    if path in ("error", "cancel"):
        owner._run_agent.side_effect = (
            RuntimeError("early run failure") if path == "error" else asyncio.CancelledError()
        )
    if path == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await fn(owner, event, source, "session", 1)
        owner._clear_session_env.assert_called_once()
        hooks.on_message_aborted.assert_called_with(message_id="inbound")
        hooks.on_message_completed_wait.assert_not_called()
        return
    response = await fn(owner, event, source, "session", 1)
    if path in ("gate", "stale", "error"):
        assert response == ("error" if path == "error" else None)
        owner._clear_session_env.assert_called_once()
        hooks.on_message_completed_wait.assert_not_called()
        hooks.on_message_aborted.assert_called()
        if path == "gate":
            hooks.on_message_started.assert_not_called()
        else:
            hooks.on_message_started.assert_called_once()
        return
    owner._hmwa_agent_error_reply.assert_not_called()
    owner._clear_session_env.assert_called_once()
    assert hooks.on_message_started.call_args.kwargs == dict(message_id="inbound", chat_id="chat",
                                                            anchor_id="quoted-anchor", session_key="session")
    if silent:
        hooks.on_message_completed_wait.assert_not_awaited()
        assert response in (None, "")
    else:
        call = hooks.on_message_completed_wait.call_args.kwargs
        assert call["message_id"] == "deepest"
        assert call["duration"] >= 0
        assert call["tokens"]["input_tokens"] == 11
        assert call["answer"] == "answer" + ("\nreset" if path == "reset" else "")
        assert call["is_error"] is failed
        assert call["reconcile_answer"] is (failed or path == "reset")
        fallback = "answer\n\nFOOTER"
        if path == "reset":
            fallback = "answer\nreset\n\nFOOTER" if already_sent else "answer\n\nFOOTER\nreset"
        assert response == (("" if failed else None) if owned else fallback)


@pytest.mark.asyncio
async def test_busy_stop_after_native_interrupt(patched, hooks, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_INTERRUPT_REASON_STOP="stop"))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", NS(EphemeralReply=str))
    order = []
    owner = NS(_interrupt_and_clear_session=AsyncMock(side_effect=lambda *a, **k: order.append("native")))
    hooks.on_session_aborted.side_effect = lambda **k: order.append("card")
    fn = method(patched, "run_busy.py", "_busy_stop_command", t=lambda _: "stopped", EphemeralReply=str)
    await fn(owner, NS(), "session", context().source)
    assert order == ["native", "card"]
    hooks.on_session_aborted.assert_awaited_once_with(session_key="session")


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [None, RuntimeError, asyncio.CancelledError])
async def test_recursive_cleanup_aborts_child_only_on_unwind(patched, hooks, exception):
    ctx = context(_hermes_lark_followup_id="child")
    tracking = asyncio.create_task(asyncio.sleep(0))
    owner = NS(_release_running_agent_state=Mock(), _draining=False)
    fn = method(patched, "run_turn.py", "_run_agent_cleanup_turn_tasks", asyncio=asyncio)

    async def unwind():
        try:
            if exception:
                raise exception()
        finally:
            await fn(owner, ctx, progress_task=None, log_task=None, interrupt_monitor=None,
                     _notify_task=None, tracking_task=tracking, stream_task=None)

    if exception:
        with pytest.raises(exception):
            await unwind()
        assert [c.kwargs["message_id"] for c in hooks.on_message_aborted.call_args_list] == ["child", "inbound"]
    else:
        await unwind()
        hooks.on_message_aborted.assert_not_called()
    owner._release_running_agent_state.assert_called_once_with("session", run_generation=1)


@pytest.mark.asyncio
async def test_normalize_both_sources_before_routing(patched, hooks, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_is_slack_ignored_channel=lambda *a: False))
    monkeypatch.setitem(sys.modules, "gateway.session_context", NS(reset_session_vars=Mock()))
    first = NS(message_id="first", source=NS(platform="feishu", thread_id="false-thread"))
    second = NS(message_id="rewritten", source=NS(platform="feishu", thread_id="another-false-thread"))
    hooks.on_feishu_normalize.side_effect = lambda **k: setattr(k["source"], "thread_id", None)

    def rewrite(event, source):
        assert source.thread_id is None
        return second

    def authorize(source):
        assert source is second.source and source.thread_id is None
        return True

    owner = NS(_reply_anchor_for_event=lambda _: "quote", _scale_to_zero_note_real_inbound=Mock(),
               _hm_pre_gateway_dispatch_hook=rewrite, _is_user_authorized_for_source=authorize)
    fn = method(patched, "run_inbound.py", "_hm_admit_event", Platform=NS(SLACK="slack"))
    assert await fn(owner, first) == (second, second.source, False)
    assert [c.kwargs["message_id"] for c in hooks.on_feishu_normalize.call_args_list] == ["first", "rewritten"]


@pytest.mark.parametrize("owned", [True, False])
def test_card_interim_rejection_does_not_start_native_delivery(patched, hooks, monkeypatch, owned):
    stream = Mock()
    monkeypatch.setitem(sys.modules, "gateway.stream_consumer", NS(GatewayStreamConsumer=Mock(return_value=stream)))
    ctx = context(interim_assistant_messages_enabled=True)
    owner = NS(_ctx=ctx, _runner=NS(config=NS(streaming=object()), _adapter_for_source=lambda _: object(),
                                   _build_stream_consumer_config=lambda *a, **k: ({}, None)))
    hooks.on_thinking_delta.return_value = owned
    _, _, interim, enabled = method(patched, "run_turn_runner.py", "_setup_stream_consumer")(owner, "feishu")
    assert enabled
    interim("thought")
    stream.on_commentary.assert_not_called()
    hooks.on_thinking_delta.assert_called_once_with(message_id="inbound", text="thought")
    interim("boundary", already_streamed=True)
    stream.on_segment_break.assert_not_called()
    assert hooks.on_thinking_delta.call_count == 1


def test_tts_error_does_not_replay_delta_to_native(patched, hooks):
    voice = Mock()
    voice.on_delta.side_effect = RuntimeError("TTS failed")
    hooks.on_answer_delta.return_value = True
    ctx = context(streaming_tts_consumer_holder=[voice])
    owner = NS(_ctx=ctx, _runner=NS(config=NS(streaming=object())))
    _, delta, _, _ = method(patched, "run_turn_runner.py", "_setup_stream_consumer")(owner, "feishu")
    delta("answer")
    voice.on_delta.assert_called_once_with("answer")


@pytest.mark.parametrize("card_error", [False, True])
def test_card_interim_preserves_tts_boundaries(hooks, card_error):
    generated = {"run_turn_runner.py": patched_gateway("run_turn_runner.py")}
    voice = Mock()
    ctx = context(streaming_tts_consumer_holder=[voice])
    owner = NS(_ctx=ctx, _runner=NS(config=NS(streaming=object())))
    if card_error:
        hooks.on_thinking_delta.side_effect = RuntimeError("card unavailable")
    _, delta, interim, enabled = method(generated, "run_turn_runner.py", "_setup_stream_consumer")(
        owner, "feishu")
    assert enabled
    delta("streamed")
    interim("streamed", already_streamed=True)
    interim("commentary")
    assert [call.args for call in voice.on_delta.call_args_list] == [
        ("streamed",), (None,), (None,), ("commentary",), (None,),
    ]
    ctx._run_still_current.return_value = False
    interim("stale")
    assert voice.on_delta.call_count == 5
    hooks.on_thinking_delta.assert_called_once_with(message_id="inbound", text="commentary")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_inbound_finally_preserves_cleanup(hooks, monkeypatch, outcome):
    generated = {"run_inbound.py": patched_gateway("run_inbound.py")}
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_AGENT_PENDING_SENTINEL=object()))
    event = NS(message_id="inbound")
    source_obj = context().source
    order = []
    hooks.on_message_aborted.side_effect = lambda **kw: order.append(("card", kw["message_id"]))
    owner = NS(
        _hm_admit_event=AsyncMock(return_value=(event, source_obj, True)),
        _hm_estop_gate=lambda *a: None, _session_key_for_source=lambda _: "session",
        _hm_pending_reply_intercepts=AsyncMock(return_value=None), _hm_evict_idle_stale_agent=Mock(),
        _is_session_running=lambda _: False,
        _hm_dispatch_idle_commands=AsyncMock(return_value=(False, None)),
        _claim_active_session_slot=lambda *a: (None, None),
        _hm_rescue_orphaned_fifo=lambda *a: (event, source_obj, True),
        _session_state=lambda _: NS(turn=NS()), _persist_active_agents=Mock(),
        _begin_session_run_generation=lambda _: 17,
        _handle_message_with_agent=AsyncMock(return_value="answer"), _run_post_turn_hooks=AsyncMock(),
        _restore_moa_one_shot=lambda event, key: order.append(("moa", event.message_id, key)),
        _restore_pending_one_turn_model_override=lambda key: order.append(("restore", key)),
        _clear_durable_active_turn=AsyncMock(side_effect=lambda e: order.append(("durable", e.message_id))),
        _release_running_agent_state=lambda key: order.append(("release", key)),
        _release_turn_lease=lambda key, gen: order.append(("lease", key, gen)),
    )
    fn = method(generated, "run_inbound.py", "_handle_message",
                TurnLeaseTimeoutError=type("LeaseError", (Exception,), {}))
    if outcome == "success":
        assert await fn(owner, event) == "answer"
    else:
        error = RuntimeError if outcome == "error" else asyncio.CancelledError
        owner._handle_message_with_agent.side_effect = error()
        with pytest.raises(error):
            await fn(owner, event)
    assert order == [("card", "inbound"), ("moa", "inbound", "session"), ("restore", "session"),
                     ("durable", "inbound"), ("release", "session"), ("lease", "session", 17)]


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
@pytest.mark.parametrize("media", [True, False])
async def test_background_inner_delivery_keeps_attachments(patched, hooks, monkeypatch, owned, media):
    monkeypatch.setitem(sys.modules, "gateway.run", NS(
        _checkpoint_agent_kwargs=Mock(), _current_max_iterations=lambda: 10,
        _load_gateway_config=lambda: {}, _platform_config_key=lambda _: "feishu",
    ))
    monkeypatch.setitem(sys.modules, "run_agent", NS(AIAgent=Mock()))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base",
                        NS(should_send_media_as_audio=lambda *a: False, EphemeralReply=str))
    monkeypatch.setitem(sys.modules, "gateway.run_notifications", NS(_IMAGE_EXTS=set(), _VIDEO_EXTS=set()))
    hooks.on_background_deliver.return_value = owned
    adapter = NS(extract_media=lambda _: ([("/tmp/file.pdf", False)] if media else [], "answer"),
                 extract_images=lambda _: ([("image-url", "caption")] if media else [], "answer"),
                 send=AsyncMock(), send_image=AsyncMock(), send_document=AsyncMock(),
                 send_video=AsyncMock(), send_image_file=AsyncMock())
    owner = NS(_adapter_for_source=lambda _: adapter, _thread_metadata_for_source=lambda *a: {},
               _resolve_session_agent_runtime=lambda **k: ("model", {"api_key": "test"}),
               _resolve_turn_toolsets=lambda *a: ([], []), _provider_routing={},
               _resolve_session_reasoning_config=lambda **k: {}, _resolve_session_service_tier=lambda **k: None,
               _resolve_turn_agent_config=lambda *a: {},
               _run_in_executor_with_context=AsyncMock(return_value={"final_response": "answer"}))
    fn = method(patched, "run_turn.py", "_run_background_task_inner", os=os,
                repair_explicit_computer_use_media_paths=lambda text, _: text,
                BasePlatformAdapter=NS(filter_media_delivery_paths=lambda files: files))
    await fn(owner, "prompt", context().source, "task", event_message_id="quote")
    hooks.on_background_deliver.assert_awaited_once_with(chat_id="chat", preview="prompt", content="answer",
                                                        reply_to_message_id="quote")
    assert adapter.send.call_count == int(not owned)
    assert adapter.send_image.call_count == int(media)
    assert adapter.send_document.call_count == int(media)
