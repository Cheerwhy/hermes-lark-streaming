"""Exercise known queued identity layouts and reject unknown upstream semantics."""

from __future__ import annotations

import ast
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_sources import SPLIT_LEDGER_REVISION, SPLIT_REVISION, source_at
from test_split_gateway import context, method

from hermes_lark_streaming.patcher import MARKERS, PatcherError, _clean_hooks
from hermes_lark_streaming.split_gateway import inject_gateway


@pytest.fixture(params=[SPLIT_REVISION, SPLIT_LEDGER_REVISION], ids=["injected-id", "upstream-id"])
def turn_source(request):
    return source_at("gateway/run_turn.py", request.param)


def test_known_identity_layouts_round_trip_without_duplicate_keywords(turn_source):
    generated = inject_gateway("run_turn.py", turn_source)
    compile(generated, "run_turn.py", "exec")
    restored = _clean_hooks(generated, MARKERS)
    assert restored == turn_source
    assert inject_gateway("run_turn.py", restored) == generated
    fn = next(n for n in ast.walk(ast.parse(generated))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_agent_queued_followup")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and ast.unparse(n.func) == "self._run_agent"]
    assert len(calls) == 1
    assert sum(k.arg == "inbound_message_id" for k in calls[0].keywords) == 1
    if "inbound_message_id=next_inbound_id" in turn_source:
        assert "inbound_message_id=next_inbound_id" in generated
        assert "inbound_message_id=getattr(pending_event" not in generated


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_nested_followups_keep_raw_identity_separate_from_reply_anchor(turn_source, monkeypatch, interrupted):
    hooks = NS(on_message_started=Mock(), on_message_interrupted=Mock(), on_message_aborted=Mock(),
               on_queued_followup_result=Mock(side_effect=lambda **kw: kw["followup_result"].setdefault(
                   "_hermes_lark_completion_id", kw["message_id"])))
    monkeypatch.setitem(sys.modules, "hermes_lark_streaming.patch", hooks)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", NS(merge_pending_message_event=Mock()))
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_preserve_queued_followup_history_offset=lambda old, new: new))
    fn = method({"run_turn.py": inject_gateway("run_turn.py", turn_source)},
                "run_turn.py", "_run_agent_queued_followup")
    calls = []
    owner = NS(_MAX_INTERRUPT_DEPTH=10, _is_goal_continuation_event=lambda _: False,
               _session_key_for_source=lambda _: "session", _reply_anchor_for_event=lambda event: event.anchor,
               _prepare_profile_scoped_inbound_message_text=AsyncMock(return_value="next"),
               _adapter_for_source=lambda _: None, _refresh_agent_cache_message_count=AsyncMock(),
               _run_agent_deliver_first_response=AsyncMock())

    async def run_agent(**kwargs):
        calls.append(kwargs)
        if kwargs["inbound_message_id"] == "B":
            ctx = context(inbound_message_id="B", event_message_id="quote-B", session_id="sid", history=[],
                          _interrupt_depth=1, context_prompt="prompt")
            return await fn(owner, ctx, None, "C", NS(message_id="C", anchor="quote-C"), {},
                            {"messages": [], "interrupted": interrupted}, None)
        return {"final_response": "C answer"}

    owner._run_agent = run_agent
    ctx = context(inbound_message_id="A", event_message_id="quote-A", session_id="sid", history=[],
                  _interrupt_depth=0, context_prompt="prompt")
    result = await fn(owner, ctx, None, "B", NS(message_id="B", anchor="quote-B"), {},
                      {"messages": [], "interrupted": interrupted}, None)
    assert result["_hermes_lark_completion_id"] == "C"
    if "queued_terminal_inbound_id" in turn_source:
        assert result["queued_terminal_inbound_id"] == "C"
    assert [(c["inbound_message_id"], c["event_message_id"]) for c in calls] == [("B", "quote-B"), ("C", "quote-C")]
    starts = hooks.on_message_interrupted if interrupted else hooks.on_message_started
    identity_key = "new_message_id" if interrupted else "message_id"
    assert [c.kwargs[identity_key] for c in starts.call_args_list] == ["B", "C"]
    assert [c.kwargs["anchor_id"] for c in starts.call_args_list] == ["quote-B", "quote-C"]


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [None, NS(anchor="quote"), NS(message_id=None, anchor="quote"),
                                   NS(message_id="", anchor="quote")], ids=["no-event", "missing", "none", "empty"])
async def test_missing_inbound_identity_does_not_become_reply_anchor(turn_source, monkeypatch, event):
    monkeypatch.setitem(sys.modules, "hermes_lark_streaming.patch", NS(
        on_message_started=Mock(), on_message_interrupted=Mock(), on_message_aborted=Mock(),
        on_queued_followup_result=Mock(),
    ))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", NS(merge_pending_message_event=Mock()))
    monkeypatch.setitem(sys.modules, "gateway.run", NS(_preserve_queued_followup_history_offset=lambda old, new: new))
    fn = method({"run_turn.py": inject_gateway("run_turn.py", turn_source)},
                "run_turn.py", "_run_agent_queued_followup")
    owner = NS(_MAX_INTERRUPT_DEPTH=10, _is_goal_continuation_event=lambda _: False,
               _session_key_for_source=lambda _: "session", _reply_anchor_for_event=lambda item: item.anchor,
               _prepare_profile_scoped_inbound_message_text=AsyncMock(return_value="next"),
               _adapter_for_source=lambda _: None, _refresh_agent_cache_message_count=AsyncMock(),
               _run_agent_deliver_first_response=AsyncMock(),
               _run_agent=AsyncMock(return_value={"final_response": "done"}))
    ctx = context(session_id="sid", history=[], _interrupt_depth=0, context_prompt="prompt")
    await fn(owner, ctx, None, "next", event, {}, {"messages": []}, None)
    call = owner._run_agent.await_args.kwargs
    assert not call["inbound_message_id"]
    assert call["event_message_id"] == ("quote" if event is not None else None)


@pytest.mark.parametrize("old,new", [
    ("inbound_message_id=next_inbound_id", "inbound_message_id=next_message_id"),
    ("next_inbound_id = None", "next_inbound_id = turn_ctx.inbound_message_id"),
    ("next_inbound_id = str(pending_event.message_id)", "next_inbound_id = str(pending_event.reply_to_message_id)"),
    ("event_message_id=next_message_id, inbound_message_id=next_inbound_id,", "**unknown_turn_kwargs,"),
    ("next_inbound_id = None", "next_inbound_id = None; next_inbound_id = 'unrelated'"),
    ("next_inbound_id = None", "next_inbound_id = None; next_inbound_id += 'unrelated'"),
])
def test_unknown_identity_mapping_fails_closed(old, new):
    source = source_at("gateway/run_turn.py", SPLIT_LEDGER_REVISION)
    assert old in source
    changed = source.replace(old, new)
    compile(changed, "changed_run_turn.py", "exec")
    with pytest.raises(PatcherError):
        inject_gateway("run_turn.py", changed)
