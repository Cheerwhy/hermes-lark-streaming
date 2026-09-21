"""Real hook/controller and upstream delivery integration, with network transports isolated."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
from hermes_sources import SPLIT_LEDGER_REVISION, SPLIT_REVISION, TARGET_REVISION, source_at
from test_split_gateway import context, method, wire_owner

import hermes_lark_streaming.patch as hooks
from hermes_lark_streaming.controller import StreamCardController
from hermes_lark_streaming.feishu import FeishuAPIError
from hermes_lark_streaming.split_gateway import inject_gateway


@pytest.fixture(scope="module", params=[SPLIT_REVISION, SPLIT_LEDGER_REVISION, TARGET_REVISION],
                ids=["split", "split-ledger", "target"])
def upstream_sources(request):
    names = [
        "gateway/run_turn_runner.py", "gateway/run_turn.py", "gateway/run_notifications.py",
        "gateway/stream_consumer.py",
        "gateway/stream_consumer_transport.py", "gateway/stream_consumer_fallback.py",
        "gateway/stream_consumer_think.py", "gateway/stream_consumer_fences.py",
        "gateway/response_filters.py", "agent/think_scrubber.py", "agent/stream_delivery.py",
    ]
    return {name: source_at(name, request.param) for name in names}


@pytest.fixture
def upstream(upstream_sources, monkeypatch):
    def module(name, **attrs):
        loaded = ModuleType(name)
        loaded.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, loaded)
        return loaded

    for name in ("gateway", "gateway.platforms", "agent"):
        module(name, __path__=[])

    # Provider setup and platform formatting are not under test. The consumer, its
    # transports/filters/finalizer, and agent delivery bookkeeping execute unmodified.
    def plain_text(text):
        assert isinstance(text, str) and "MEDIA:" not in text and "<think>" not in text
        return text

    module("gateway.platforms.base", BasePlatformAdapter=type("BasePlatformAdapter", (), {
        "strip_media_directives_for_display": staticmethod(plain_text),
    }), _custom_unit_to_cp=lambda text, budget, len_fn: min(len(text), budget))
    module("gateway.config", DEFAULT_STREAMING_EDIT_INTERVAL=0,
           DEFAULT_STREAMING_BUFFER_THRESHOLD=1, DEFAULT_STREAMING_CURSOR="")
    module("agent.memory_manager", sanitize_context=plain_text)
    module("agent.message_content", flatten_message_text=plain_text)
    module("agent.redact", redact_sensitive_text=plain_text)
    for name in (
        "agent.think_scrubber", "agent.stream_delivery", "gateway.response_filters",
        "gateway.stream_consumer_fences", "gateway.stream_consumer_transport",
        "gateway.stream_consumer_fallback", "gateway.stream_consumer_think", "gateway.stream_consumer",
    ):
        loaded = module(name)
        exec(compile(upstream_sources[name.replace(".", "/") + ".py"], name, "exec"), loaded.__dict__)

    agent_class = type("DeliveryAgent", (sys.modules["agent.stream_delivery"].StreamDeliveryMixin,), {
        "_stream_callback": None, "_strip_think_blocks": staticmethod(plain_text),
    })
    generated = {
        name: inject_gateway(name, upstream_sources[f"gateway/{name}"])
        for name in ("run_turn_runner.py", "run_turn.py")
    }
    generated["run_notifications.py"] = upstream_sources["gateway/run_notifications.py"]
    consumer_module = sys.modules["gateway.stream_consumer"]
    return NS(
        generated=generated, Agent=agent_class, Consumer=consumer_module.GatewayStreamConsumer,
        ConsumerConfig=consumer_module.StreamConsumerConfig,
        original_runner=upstream_sources["gateway/run_turn_runner.py"],
    )


@pytest.fixture
def controller(tmp_path, monkeypatch):
    ctrl = StreamCardController(profile_home=tmp_path)
    ctrl._cfg._raw = {
        "streaming": {"enabled": True, "footer": {"enabled": False}},
        "feishu": {"app_id": "test-app", "app_secret": "test-secret"},
    }
    ctrl._initialized = True
    ctrl._client = NS(
        cardkit_create=AsyncMock(return_value="card"), reply_card_by_id=AsyncMock(return_value="card-message"),
        cardkit_close_streaming=AsyncMock(), cardkit_update=AsyncMock(),
    )
    monkeypatch.setattr(hooks, "get_controller", lambda: ctrl)
    return ctrl


def setup(upstream, *, native=True, interim=True, tts=None, platform="feishu"):
    adapter = NS(
        MAX_MESSAGE_LENGTH=10000, SUPPORTS_MESSAGE_EDITING=True,
        send=AsyncMock(return_value=NS(success=True, message_id="native-message")),
        edit_message=AsyncMock(return_value=NS(success=True)),
    )
    ctx = context(
        platform=platform, result_holder=[None], streaming_tts_consumer_holder=[tts],
        resolve_display_setting=lambda *a: native, interim_assistant_messages_enabled=interim,
    )
    owner = NS(_ctx=ctx, _runner=NS(
        config=NS(streaming=NS(enabled=native, transport="auto")),
        _adapter_for_source=lambda _: adapter,
        _build_stream_consumer_config=lambda *a, **k: (upstream.ConsumerConfig(), None),
    ))
    consumer, delta, interim_cb, want_interim = method(
        upstream.generated, "run_turn_runner.py", "_setup_stream_consumer",
    )(owner, platform)
    agent = upstream.Agent()
    agent.stream_delta_callback = delta
    agent.interim_assistant_callback = interim_cb if want_interim else None
    return NS(owner=owner, ctx=ctx, adapter=adapter, consumer=consumer, agent=agent)


async def finish_worker(upstream, turn, final, **flags):
    result = {"final_response": final, "completed": True, "messages": [], **flags}
    method(upstream.generated, "run_turn_runner.py", "_finish_stream_consumer",
           repair_explicit_computer_use_media_paths=lambda text, *a, **k: text)(
        turn.owner, result, [], turn.consumer,
    )
    assert turn.ctx.result_holder[0] is result
    if turn.consumer is not None:
        await turn.consumer.run()
    confirmed = method(upstream.generated, "run_turn.py", "_run_agent_stream_confirmed_final_delivery")
    owner = NS(_run_agent_stream_confirmed_final_delivery=confirmed)
    await method(upstream.generated, "run_turn.py", "_run_agent_mark_streamed_delivery")(
        owner, result, turn.ctx,
    )
    return result


async def finish_gateway(upstream, turn, result, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run_heartbeat_acceptance",
                        NS(heartbeat_owner_is_current=lambda *a: True))
    event = NS(message_id="inbound", text="question", channel_prompt=None, message_type="text")
    source = turn.ctx.source
    source.user_id, source.user_name, source.thread_id = "user", "user", None
    prepared = NS(
        history=[], message_text="question", context_prompt="", persist_user_message="question",
        persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner="owner",
    )
    owner = NS(
        _PreparedTurn=NS,
        _hmwa_resolve_session=AsyncMock(return_value=(source, NS(session_id="sid"), "session")),
        _hmwa_prepare_turn=AsyncMock(return_value=(prepared, [])), hooks=NS(emit=AsyncMock()),
        _reply_anchor_for_event=lambda _: "quoted-anchor", _run_agent=AsyncMock(return_value=result),
        _hmwa_stop_typing_for_turn=AsyncMock(), _is_session_run_current=lambda *a: True,
        _hmwa_shape_agent_response=AsyncMock(return_value=(result["final_response"], False, [])),
        _hmwa_prepend_reasoning=lambda r, text, *a: text, _hmwa_runtime_footer_line=lambda *a: "",
        _hmwa_post_turn_hooks=AsyncMock(), _hmwa_classify_turn_failure=lambda *a: (False, False, False),
        _hmwa_compression_exhaustion_reset=AsyncMock(side_effect=lambda r, text, e, *a: (text, e)),
        _hmwa_persist_turn_transcript=AsyncMock(), _hmwa_agent_error_reply=AsyncMock(return_value="unexpected error"),
        _clear_session_env=Mock(), _adapter_for_source=lambda _: turn.adapter,
        _should_send_voice_reply=lambda *a, **k: False, _deliver_media_from_response=AsyncMock(),
    )
    delivery = method(upstream.generated, "run_turn.py", "_hmwa_deliver_turn_response")
    owner._hmwa_deliver_turn_response = lambda *a: delivery(owner, *a)
    response = await method(upstream.generated, "run_turn.py", "_handle_message_with_agent")(
        owner, event, source, "session", 1,
    )
    owner._hmwa_agent_error_reply.assert_not_called()
    return response, owner


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("tts", [False, True])
async def test_disabled_plugin_keeps_native_interim_bookkeeping(upstream, controller, native, tts):
    controller._cfg._raw["streaming"]["enabled"] = False
    voice = NS(on_delta=Mock()) if tts else None
    turn = setup(upstream, native=native, tts=voice)
    assert isinstance(turn.consumer, upstream.Consumer)
    assert (turn.agent.stream_delta_callback is None) is (not native and not tts)
    turn.agent._fire_stream_delta("I will investigate the failure.")
    turn.agent._emit_interim_assistant_message({"content": "I will investigate the failure."})
    tick = turn.consumer._drain_queue()
    if not native and not tts:
        assert tick.commentary_text == "I will investigate the failure."
        assert not tick.got_segment_break
        assert not turn.agent._current_streamed_assistant_text
    if voice:
        # Disabled hooks must preserve that revision's native TTS behavior, including
        # the interim flush boundary introduced after the original split release.
        native_voice = NS(on_delta=Mock())
        turn.ctx.streaming_tts_consumer_holder = [native_voice]
        _, delta, interim, _ = method(
            {"run_turn_runner.py": upstream.original_runner}, "run_turn_runner.py", "_setup_stream_consumer",
        )(turn.owner, "feishu")
        delta("I will investigate the failure.")
        interim("I will investigate the failure.", already_streamed=True)
        assert voice.on_delta.call_args_list == native_voice.on_delta.call_args_list
    turn.consumer.finish()
    await turn.consumer.run()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("tts", [False, True])
async def test_no_native_sinks_and_card_registration(upstream, controller, registered, tts):
    if registered:
        hooks.on_message_started(message_id="inbound", chat_id="chat")
    voice = NS(on_delta=Mock()) if tts else None
    turn = setup(upstream, native=False, interim=False, tts=voice)
    assert turn.consumer is None
    assert (turn.agent.stream_delta_callback is None) is (not registered and not tts)
    assert (turn.agent.interim_assistant_callback is not None) is registered
    turn.agent._fire_stream_delta("Answer.")
    if turn.agent.stream_delta_callback is not None:
        turn.agent.stream_delta_callback(None)
        turn.ctx._run_still_current.return_value = False
        turn.agent.stream_delta_callback("stale")
    if voice:
        assert [c.args for c in voice.on_delta.call_args_list] == [("Answer.",), (None,)]
    if registered:
        assert await hooks.on_message_completed_wait(message_id="inbound", answer="Answer.")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("tts", [False, True])
async def test_mixed_reasoning_answer_has_one_final_transport(upstream, controller, monkeypatch, native, tts):
    hooks.on_message_started(message_id="inbound", chat_id="chat", anchor_id="quoted-anchor")
    voice = NS(on_delta=Mock()) if tts else None
    turn = setup(upstream, native=native, tts=voice)
    turn.agent._fire_stream_delta("Reasoning:\nLet me calculate.")
    assert not controller._sessions["inbound"].segment_state.segments
    turn.agent._fire_stream_delta("\n\nThe answer is 42.")
    assert controller._sessions["inbound"].segment_state.segments[-1].text == "\n\nThe answer is 42."
    final = "Reasoning:\nLet me calculate.\n\nThe answer is 42."
    result = await finish_worker(upstream, turn, final)
    assert not result.get("already_sent")
    response, owner = await finish_gateway(upstream, turn, result, monkeypatch)
    assert response is None and result["already_sent"]
    card = controller._client.cardkit_update.call_args.args[1]
    controller._client.cardkit_update.assert_awaited_once()
    assert json.dumps(card["body"]).count("The answer is 42.") == 1
    turn.adapter.send.assert_not_awaited()
    turn.adapter.edit_message.assert_not_awaited()
    assert turn.consumer is None and turn.ctx.stream_consumer_holder == [None]
    owner._deliver_media_from_response.assert_awaited_once()
    assert not controller._sessions
    if voice:
        assert [c.args[0] for c in voice.on_delta.call_args_list] == [
            "Reasoning:\nLet me calculate.", "\n\nThe answer is 42.",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("deltas", [[], ["Reasoning:\nLet me calculate."]])
async def test_zero_accepted_deltas_still_has_one_final_delivery(upstream, controller, monkeypatch, deltas):
    hooks.on_message_started(message_id="inbound", chat_id="chat")
    turn = setup(upstream)
    for delta in deltas:
        turn.agent._fire_stream_delta(delta)
    assert not controller._sessions["inbound"].segment_state.segments
    result = await finish_worker(upstream, turn, "Final answer without accepted deltas.")
    response, _ = await finish_gateway(upstream, turn, result, monkeypatch)
    assert response is None and result["already_sent"]
    assert "Final answer without accepted deltas." in json.dumps(controller._client.cardkit_update.call_args.args[1])
    turn.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["creation_before_setup", "creation_after_setup", "finalization"])
async def test_card_failures_leave_full_native_final_fallback(upstream, controller, monkeypatch, failure):
    if failure.startswith("creation"):
        controller._client.cardkit_create.side_effect = FeishuAPIError("test creation failure", code=999999)
    else:
        controller._client.cardkit_update.side_effect = FeishuAPIError("test finalization failure", code=999999)
    hooks.on_message_started(message_id="inbound", chat_id="chat")
    session = controller._sessions["inbound"]
    if failure == "creation_before_setup":
        await session.create_task
        assert session.state.is_terminal
    turn = setup(upstream)
    turn.agent._fire_stream_delta("Partial answer.")
    result = await finish_worker(upstream, turn, "Full final answer. MEDIA:/tmp/result.pdf")
    assert not result.get("already_sent")
    response, _ = await finish_gateway(upstream, turn, result, monkeypatch)
    assert response == "Full final answer. MEDIA:/tmp/result.pdf"
    assert not result.get("already_sent")
    # The outer adapter owns this returned fallback; nothing streamed it beforehand.
    turn.adapter.send.assert_not_awaited()
    turn.adapter.edit_message.assert_not_awaited()
    assert not controller._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "creation", "finalization"])
async def test_queued_completion_confirms_only_successful_card_delivery(upstream, controller, monkeypatch, failure):
    if failure:
        api_call = controller._client.cardkit_create if failure == "creation" else controller._client.cardkit_update
        api_call.side_effect = FeishuAPIError("test card failure", code=999999)
    hooks.on_message_started(message_id="inbound", chat_id="chat")
    turn = setup(upstream)
    turn.agent._fire_stream_delta("Reasoning:\nLet me calculate.")
    turn.agent._fire_stream_delta("\n\nQueued answer.")
    result = await finish_worker(upstream, turn, "Queued answer.")
    raw = dict(result)
    monkeypatch.setitem(sys.modules, "gateway.run", NS(
        _strip_response_attachments_for_direct_send=lambda text, adapter: text,
    ))
    owner = NS(
        _run_agent_stream_confirmed_final_delivery=method(
            upstream.generated, "run_turn.py", "_run_agent_stream_confirmed_final_delivery",
        ),
        _is_intentional_silence=lambda *a: False, _pop_post_delivery_callback=lambda *a: None,
        _deliver_media_from_response=AsyncMock(),
    )
    delivery = method(upstream.generated, "run_notifications.py", "_deliver_queued_first_response", MessageEvent=NS)
    owner._deliver_queued_first_response = lambda *a, **k: delivery(owner, *a, **k)
    if "async def _send_queued_final_text(" in upstream.generated["run_notifications.py"]:
        send = method(upstream.generated, "run_notifications.py", "_send_queued_final_text",
                      BasePlatformAdapter=sys.modules["gateway.platforms.base"].BasePlatformAdapter)
        owner._send_queued_final_text = lambda *a, **k: send(owner, *a, **k)
    await method(upstream.generated, "run_turn.py", "_run_agent_deliver_first_response")(
        owner, turn.ctx, turn.adapter, result, raw, None,
    )
    assert bool(result.get("already_sent")) is (failure is None)
    assert bool(raw.get("response_previewed")) is (failure is None)
    assert turn.adapter.send.await_count == int(failure is not None)
    if failure:
        assert turn.adapter.send.call_args.args[1] == "Queued answer."
    owner._deliver_media_from_response.assert_awaited_once()
    assert owner._deliver_media_from_response.call_args.args[0] == "Queued answer."
    turn.adapter.edit_message.assert_not_awaited()
    assert not controller._sessions


@pytest.mark.asyncio
async def test_worker_callback_failure_keeps_tts_single_and_final_native_silent(upstream, controller, monkeypatch):
    hooks.on_message_started(message_id="inbound", chat_id="chat")
    await controller._sessions["inbound"].create_task
    voice = NS(on_delta=Mock(side_effect=RuntimeError("TTS unavailable")))
    turn = setup(upstream, tts=voice)
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    loop.set_debug(True)
    try:
        # Exercise the inherited off-loop flush exception without changing its implementation.
        await asyncio.to_thread(turn.agent._fire_stream_delta, "Worker answer.")
    finally:
        loop.set_debug(previous_debug)
    voice.on_delta.assert_called_once_with("Worker answer.")
    result = await finish_worker(upstream, turn, "Worker answer.")
    response, _ = await finish_gateway(upstream, turn, result, monkeypatch)
    assert response is None and result["already_sent"]
    turn.adapter.send.assert_not_awaited()
    turn.adapter.edit_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,enabled", [("feishu", False), ("telegram", True)])
async def test_native_finalization_and_confirmation_unchanged(upstream, controller, platform, enabled):
    controller._cfg._raw["streaming"]["enabled"] = enabled
    turn = setup(upstream, platform=platform)
    assert isinstance(turn.consumer, upstream.Consumer)
    turn.agent._fire_stream_delta("Final")
    result = await finish_worker(upstream, turn, "Final authoritative response.")
    assert result["already_sent"]
    assert turn.consumer.final_content_delivered
    assert turn.consumer.delivered_final_matches(result["final_response"])
    turn.adapter.send.assert_awaited_once()
    assert turn.adapter.send.call_args.kwargs["content"] == "Final authoritative response."


@pytest.mark.asyncio
async def test_cached_agent_rebinds_to_disabled_native_callbacks(upstream, controller, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.run", NS(
        _interim_metadata=lambda m: m, _non_conversational_metadata=lambda m, **k: m,
    ))
    hooks.on_message_started(message_id="inbound", chat_id="chat")
    agent = upstream.Agent()
    wire = method(upstream.generated, "run_turn_runner.py", "_wire_turn_agent_callbacks")
    for enabled in (True, False):
        controller._cfg._raw["streaming"]["enabled"] = enabled
        turn = setup(upstream, native=False, interim=False)
        owner = wire_owner(upstream.generated, turn.ctx)
        wire(owner, agent, {}, {}, turn.agent.stream_delta_callback,
             turn.agent.interim_assistant_callback, enabled)
        assert (agent.stream_delta_callback is not None) is enabled
        assert (agent.interim_assistant_callback is not None) is enabled
    controller._cfg._raw["streaming"]["enabled"] = True
    await hooks.on_message_completed_wait(message_id="inbound", answer="Finished.")
