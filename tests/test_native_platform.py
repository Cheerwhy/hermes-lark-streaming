"""在实际 Hermes 0.22.0 协议上验证，不用自造流式消费者替代宿主."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

pytest.importorskip("gateway.stream_consumer", reason="Requires a Hermes >= 0.22.0 checkout")

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from gateway.session_context import reset_session_vars, set_session_vars
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import parse_manifest_file

from hermes_lark_streaming.adapter import ACTIVE, FeishuStreamingAdapter, builtin
from hermes_lark_streaming.feishu import FeishuClient
from hermes_lark_streaming.hooks import observe_reasoning, observe_start
from hermes_lark_streaming.plugin import register
from hermes_lark_streaming.sender import standalone_send

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def adapter(tmp_path):
    config = PlatformConfig(enabled=True, extra={"app_id": "app", "app_secret": "secret"})
    value = FeishuStreamingAdapter(config)
    value.cards._client = create_autospec(FeishuClient, instance=True)
    value.cards._client.cardkit_create.return_value = "card-id"
    value.cards._client.reply_card_by_id.return_value = "message-id"
    value.cards._client.send_card_to_chat.return_value = "message-id"
    yield value
    ACTIVE.discard(value)
    reset_session_vars()


def test_register_uses_real_platform_registry_and_restores_builtin(tmp_path):
    from gateway.platform_registry import platform_registry

    manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    assert manifest.requires_hermes == ">=0.22.0"
    manager = PluginManager()
    ctx = PluginContext(manifest, manager)
    previous = platform_registry.snapshot_registration("feishu", scope=manager.scope_key)
    register(ctx)
    entry = platform_registry.get("feishu")
    assert entry.adapter_factory is FeishuStreamingAdapter
    assert entry.allowed_users_env == "FEISHU_ALLOWED_USERS"
    assert entry.standalone_sender_fn is standalone_send
    # ledger 的正式注销恢复原注册，不修改核心对象或函数
    manager.unload(manifest)
    assert platform_registry.snapshot_registration("feishu", scope=manager.scope_key) == previous


@pytest.mark.asyncio
async def test_real_consumer_stream_and_final(adapter):
    set_session_vars(platform="feishu", chat_id="chat", message_id="incoming", session_key="session")
    consumer = GatewayStreamConsumer(
        adapter,
        "chat",
        config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
        initial_reply_to_id="incoming",
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("你好")
    await asyncio.sleep(0.03)
    consumer.on_delta("世界")
    consumer.finish("你好世界")
    await asyncio.wait_for(task, 2)
    assert consumer.final_content_delivered
    assert adapter.cards._client.cardkit_create.call_count == 1
    assert not adapter.cards.turns
    assert "你好世界" in str(adapter.cards._client.cardkit_update.call_args)


@pytest.mark.asyncio
async def test_real_consumer_failed_card_uses_static_fallback(adapter):
    adapter.cards._client.cardkit_create.side_effect = RuntimeError("cardkit unavailable")
    consumer = GatewayStreamConsumer(
        adapter,
        "chat",
        config=StreamConsumerConfig(edit_interval=0.01, cursor=""),
        initial_reply_to_id="in",
    )
    consumer.on_delta("answer")
    consumer.finish("answer")
    await asyncio.wait_for(consumer.run(), 2)
    adapter.cards._client.send_card_to_chat.assert_awaited()


@pytest.mark.asyncio
async def test_quoted_reply_keeps_inbound_identity(adapter):
    event = MessageEvent(
        text="question",
        source=SessionSource(platform=Platform.FEISHU, chat_id="chat", user_id="u"),
        message_id="incoming",
        reply_to_message_id="quoted",
        raw_message={"message": {}},
    )
    with patch.object(builtin.FeishuAdapter, "handle_message", new=AsyncMock()):
        await adapter.handle_message(event)
    set_session_vars(platform="feishu", chat_id="chat", message_id="incoming", session_key="s")
    await adapter.send_stream_frame("", chat_id="chat", reply_to="quoted", turn_id="gateway-uuid")
    turn = adapter.cards.turns["gateway-uuid"]
    assert turn.source_id == "incoming"
    adapter.cards._client.reply_card_by_id.assert_awaited_with("quoted", "card-id")
    await adapter.interrupt_session_activity("s", "chat")
    assert not adapter.cards.turns


@pytest.mark.asyncio
async def test_observer_threads_use_original_message_context(adapter, tmp_path):
    (tmp_path / "config.yaml").write_text("display:\n  show_reasoning: true\n")
    set_session_vars(platform="feishu", chat_id="chat", message_id="incoming")
    await adapter.send_stream_frame("", chat_id="chat", reply_to="incoming", turn_id="gateway-id")
    await asyncio.to_thread(observe_start, turn_id="agent-id")
    await asyncio.to_thread(observe_reasoning, turn_id="agent-id", kind="reasoning", delta="思考")
    await asyncio.sleep(0)
    assert adapter.cards.turns["gateway-id"].session.segment_state.segments[0].text == "思考"
    await adapter.cards.close()


@pytest.mark.asyncio
async def test_context_uses_real_host_hook_and_latest_prompt_including_cache(adapter, monkeypatch):
    # 宿主对话循环会导入启动器；测试不得同步或切换用户的托管依赖环境
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    from agent.api_request_hooks import ApiRequestHooksMixin
    from agent.turn_response_intake import _fire_post_api_request_hook

    cards = adapter.cards
    await cards.frame("t", "chat", "answer", reply_to=None, source_id="in")
    agent = SimpleNamespace(
        session_id="s", platform="feishu", model="test-model", provider="openai",
        base_url="", api_mode="chat_completions", context_compressor=SimpleNamespace(context_length=80000),
        _api_response_payload_for_hook=lambda *a, **kw: {},
    )
    usage_summary = ApiRequestHooksMixin._usage_summary_for_api_request_hook
    agent._usage_summary_for_api_request_hook = lambda response: usage_summary(agent, response)
    events = []
    for index, (uncached, cached) in enumerate([(1000, 9000), (2000, 18000)]):
        response = SimpleNamespace(model="test-model", usage={
            "prompt_tokens": uncached + cached, "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": cached},
        })
        with (
            patch("hermes_cli.lifecycle.has_hook", return_value=True),
            patch("hermes_cli.lifecycle.invoke_hook") as invoke,
        ):
            _fire_post_api_request_hook(
                agent, response, SimpleNamespace(content="answer"), "stop", api_messages=[],
                api_call_count=index + 1, api_duration=1, api_start_time=1,
                api_request_id=f"request-{index}", effective_task_id="task", turn_id="agent",
            )
            assert invoke.call_args.args == ("post_api_request",)
            event = invoke.call_args.kwargs
        events.append(event)
        await cards.observe("in", "usage", event)
    await cards.observe("in", "usage", events[0])  # 重复事件不回滚最新占用或重复累计
    footer = cards.turns["t"].session.footer
    assert footer["input_tokens"] == 3000
    assert footer["prompt_tokens"] == 20000
    assert footer["context_length"] == 80000
    await cards.frame("t", "chat", "answer", reply_to=None, source_id="in", finalize=True)
    assert "20.0K/80.0K (25%)" in str(cards._client.cardkit_update.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,limit", [({}, 80000), ({"prompt_tokens": 5000}, None)])
async def test_context_does_not_reuse_missing_values_from_previous_request(adapter, usage, limit):
    cards = adapter.cards
    await cards.frame("t", "chat", "", reply_to=None, source_id="in")
    for index, (current_usage, current_limit) in enumerate([
        ({"prompt_tokens": 20000}, 80000), (usage, limit),
    ]):
        await cards.observe("in", "usage", {
            "api_request_id": str(index), "usage": current_usage, "context_length": current_limit,
        })
    await cards.frame("t", "chat", "answer", reply_to=None, source_id="in", finalize=True)
    assert "25%" not in str(cards._client.cardkit_update.call_args)


@pytest.mark.asyncio
async def test_static_card_edit_uses_card_patch(adapter):
    sent = await adapter.send("chat", "first")
    result = await adapter.edit_message("chat", sent.message_id, "updated", finalize=True)
    assert result.success
    adapter.cards._client.update_card_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_static_card_fails_back_to_builtin(adapter):
    adapter.cards._client.send_card_to_chat.side_effect = RuntimeError("card failure")
    with patch.object(builtin.FeishuAdapter, "send", new=AsyncMock(return_value=SendResult(success=True))) as send:
        result = await adapter.send("chat", "answer", metadata={"thread_id": "topic"})
    assert result.success
    assert send.call_args.kwargs["metadata"] == {"thread_id": "topic"}


@pytest.mark.asyncio
async def test_standalone_card_and_media_use_same_adapter(adapter, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"test")
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="file"))
    adapter.disconnect = AsyncMock()
    with (
        patch("hermes_lark_streaming.adapter.FeishuStreamingAdapter", return_value=adapter),
        patch.object(
            builtin,
            "_load_lark_oapi",
            return_value=True,
        ),
        patch.object(adapter, "_build_lark_client", return_value=MagicMock()),
    ):
        result = await standalone_send(
            adapter.config,
            "chat",
            "cron",
            thread_id="topic",
            media_files=[(str(image), False)],
            force_document=True,
        )
    assert result["success"]
    adapter.cards._client.send_card_to_chat.assert_awaited_once()
    assert adapter.cards._client.send_card_to_chat.call_args.kwargs["reply_to_message_id"] == "topic"
    adapter.send_document.assert_awaited_once()
    adapter.disconnect.assert_awaited_once()


def test_directory_loader_registers_and_unloads_without_source_changes(tmp_path):
    import hashlib

    import hermes_cli
    from gateway.platform_registry import platform_registry

    host = Path(hermes_cli.__file__).resolve().parent.parent
    paths = [
        host / p
        for p in [
            "gateway/run.py",
            "gateway/run_turn.py",
            "gateway/stream_consumer.py",
            "plugins/platforms/feishu/adapter.py",
            "cron/scheduler_delivery.py",
        ]
    ]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    manager = PluginManager()
    try:
        manager._load_plugin(manifest)
        loaded = manager._plugins[manifest.key or manifest.name]
        assert loaded.enabled, loaded.error
        entry = platform_registry.get("feishu")
        assert entry.adapter_factory.__name__ == "FeishuStreamingAdapter"
        assert entry.adapter_factory.__module__.startswith("hermes_plugins.")
    finally:
        manager.unload()
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reopen", [True, False], ids=["clarify", "approval"])
async def test_real_interaction_boundary_keeps_followup(adapter, reopen):
    set_session_vars(platform="feishu", chat_id="chat", message_id="in", session_key="s")
    consumer = GatewayStreamConsumer(
        adapter,
        "chat",
        config=StreamConsumerConfig(edit_interval=0.01, cursor=""),
        initial_reply_to_id="in",
    )
    task = asyncio.create_task(consumer.run())
    consumer.on_delta("before")
    await asyncio.sleep(0.02)
    await adapter.cards.observe("in", "tool_start", {
        "tool_name": "clarify", "tool_call_id": "clarify-call", "turn_id": "agent-turn",
    })
    future, _ = consumer.close_for_approval_prompt(reason="Clarify" if reopen else "Approval", reopen=reopen)
    assert await asyncio.wait_for(future, 2)
    assert "Running" in str(adapter.cards._client.cardkit_update.call_args)
    await adapter.cards.observe("in", "tool_end", {
        "tool_name": "clarify", "tool_call_id": "clarify-call", "turn_id": "agent-turn",
        "duration_ms": 1000,
    })
    assert "Succeeded" in str(adapter.cards._client.cardkit_update.call_args)
    assert "Running" not in str(adapter.cards._client.cardkit_update.call_args)
    assert adapter.cards._client.cardkit_close_streaming.await_count == 1
    consumer.on_delta("after")
    consumer.finish("after")
    await asyncio.wait_for(task, 2)
    assert not adapter.cards.turns
    if reopen:
        assert adapter.cards._client.cardkit_create.call_count == 2
        assert "after" in str(adapter.cards._client.cardkit_update.call_args)
    else:
        assert "after" in str(adapter.cards._client.send_card_to_chat.call_args)


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_delayed_tool_result_updates_original_card_after_reopen(adapter, is_error):
    cards = adapter.cards
    cards._client.cardkit_create.side_effect = ["old-card", "new-card"]
    await cards.frame("old", "chat", "question", reply_to=None, source_id="in")
    event = {"tool_name": "clarify", "tool_call_id": "call", "turn_id": "agent"}
    await cards.observe("in", "tool_start", event)
    await cards.frame("old", "chat", "question", reply_to=None, source_id="in", finalize=True)
    await cards.frame("new", "chat", "", reply_to=None, source_id="in")
    old_updates = cards._client.cardkit_update.await_count
    await cards.observe("other-source", "tool_end", event)
    await cards.observe("in", "tool_end", {**event, "turn_id": "other-agent"})
    assert cards._client.cardkit_update.await_count == old_updates
    assert not cards.turns["new"].tools

    await cards.observe("in", "tool_end", {**event, "is_error": is_error})
    update = cards._client.cardkit_update.call_args
    assert update.args[0] == "old-card"
    assert ("Failed" if is_error else "Succeeded") in str(update)
    assert "Running" not in str(update)
    assert not cards.turns["new"].tools
    assert cards._client.cardkit_close_streaming.await_count == 1
    assert not cards._pending_tools
    await cards.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("show_tools", [True, False])
@pytest.mark.parametrize("outcome", ["submitted", "cancelled", "timed_out", "undelivered", "skipped", "malformed"])
async def test_clarify_placeholder_tracks_result(adapter, monkeypatch, language, show_tools, outcome):
    from agent.i18n import t

    monkeypatch.setenv("HERMES_LANGUAGE", language)
    cards = adapter.cards
    monkeypatch.setattr(cards._cfg, "_reload", lambda: {"display": {"show_tool_use": show_tools}})
    await cards.frame("old", "chat", "", reply_to=None, source_id="in")
    event = {"tool_name": "clarify", "tool_call_id": "call"}
    await cards.observe("in", "tool_start", event)
    placeholder = t("gateway.clarify.native_stream_placeholder")
    await cards.frame("old", "chat", placeholder, reply_to=None, source_id="in", finalize=True)
    assert placeholder in str(cards._client.cardkit_update.call_args)
    result = json.dumps({
        "outcome": "submitted" if outcome == "skipped" else outcome,
        "responses": [{"status": "answered" if outcome == "submitted" else "unanswered",
                       "user_response": "private-choice"}],
    }) if outcome != "malformed" else "invalid JSON"
    await cards.observe("in", "tool_end", {**event, "result": result})
    final = str(cards._client.cardkit_update.call_args)
    expected = (
        ("已收到你的选择" if language == "zh" else "Received your choice") if outcome == "submitted"
        else ("本次澄清已结束" if language == "zh" else "Clarification ended")
    )
    assert expected in final
    assert placeholder not in final
    assert "private-choice" not in final
    assert "Running" not in final
    assert final.count("**Clarify") == int(show_tools)
    assert not cards._pending_tools


@pytest.mark.asyncio
async def test_clarify_does_not_replace_model_text(adapter):
    from agent.i18n import t

    cards = adapter.cards
    text = t("gateway.clarify.native_stream_placeholder")
    await cards.frame("old", "chat", text, reply_to=None, source_id="in")
    event = {"tool_name": "clarify", "tool_call_id": "call"}
    await cards.observe("in", "tool_start", event)
    await cards.frame("old", "chat", text, reply_to=None, source_id="in", finalize=True)
    await cards.observe("in", "tool_end", {**event, "result": '{"outcome":"cancelled"}'})
    assert text in str(cards._client.cardkit_update.call_args)


@pytest.mark.asyncio
async def test_interim_is_rendered_in_card_without_text_bubble(adapter):
    set_session_vars(platform="feishu", chat_id="chat", message_id="in", session_key="s")
    await adapter.send_stream_frame("", chat_id="chat", reply_to="in", turn_id="t")
    with patch.object(builtin.FeishuAdapter, "send", new=AsyncMock()) as native_send:
        result = await adapter.send("chat", "checking", metadata={"_interim_send": True})
    assert result.success
    native_send.assert_not_awaited()
    await adapter.send_stream_frame("answer", chat_id="chat", reply_to="in", turn_id="t", finalize=True)
    final = str(adapter.cards._client.cardkit_update.call_args)
    assert "checking" in final and "answer" in final


@pytest.mark.asyncio
async def test_native_clarify_text_card_keeps_core_reply_resolution(adapter):
    from tools.clarify_gateway import _entries, mark_awaiting_text, register, resolve_gateway_clarify

    register("clarify", "s", "Which?", ["A", "B"])
    try:
        mark_awaiting_text("clarify")
        sent = await adapter.send_clarify("chat", "Which?", ["A", "B"], "clarify", "s")
        assert sent.success
        assert _entries["clarify"].awaiting_text
        assert resolve_gateway_clarify("clarify", "A")
    finally:
        _entries.pop("clarify", None)


@pytest.mark.asyncio
async def test_standalone_initialization_failure_is_reported():
    with (
        patch("hermes_lark_streaming.adapter.FeishuStreamingAdapter", side_effect=ValueError("invalid config")),
        patch.object(
            builtin,
            "_load_lark_oapi",
            return_value=True,
        ),
    ):
        result = await standalone_send(PlatformConfig(), "chat", "cron")
    assert "error" in result and not result.get("success")


@pytest.mark.asyncio
async def test_real_consumers_in_same_chat_keep_separate_contexts(adapter):
    async def run_message(message_id, text):
        set_session_vars(platform="feishu", chat_id="chat", message_id=message_id, session_key=message_id)
        consumer = GatewayStreamConsumer(
            adapter,
            "chat",
            config=StreamConsumerConfig(edit_interval=0.01, cursor=""),
            initial_reply_to_id=message_id,
        )
        task = asyncio.create_task(consumer.run())
        consumer.on_delta(text)
        await asyncio.sleep(0.02)
        consumer.finish(text)
        await task
        assert consumer.final_content_delivered

    await asyncio.gather(run_message("user-a", "answer-a"), run_message("user-b", "answer-b"))
    assert adapter.cards._client.cardkit_create.call_count == 2
    replies = [call.args[0] for call in adapter.cards._client.reply_card_by_id.call_args_list]
    assert set(replies) == {"user-a", "user-b"}
    assert not adapter.cards.turns


def test_discovery_keeps_user_adapter_after_builtin_lazy_load(tmp_path, monkeypatch):
    from gateway.platform_registry import platform_registry

    plugin_dir = tmp_path / "plugins" / "hermes-lark-streaming"
    plugin_dir.parent.mkdir()
    plugin_dir.symlink_to(ROOT, target_is_directory=True)
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [hermes-lark-streaming]\n  load_timeout_seconds: 0\n")
    # 只模拟目录安装这一条分发路径，不让开发环境的 editable entry point 再次覆盖它
    monkeypatch.setattr("hermes_cli.plugins_discovery.discover_entrypoint_manifests", lambda: [])
    manager = PluginManager()
    try:
        manager.discover_and_load()
        loaded = manager._plugins.get("hermes-lark-streaming")
        assert loaded is not None and loaded.enabled, getattr(loaded, "error", None)
        first = platform_registry.get("feishu")
        assert first.adapter_factory.__name__ == "FeishuStreamingAdapter"
        assert platform_registry.get("feishu").adapter_factory is first.adapter_factory
        manager.unload("hermes-lark-streaming")
        restored = platform_registry.get("feishu")
        assert restored.adapter_factory.__name__ == "FeishuAdapter"
    finally:
        manager.unload()


@pytest.mark.asyncio
async def test_background_card_uses_explicit_target_without_merging_active_turn(adapter):
    set_session_vars(platform="feishu", chat_id="foreground", message_id="in", session_key="s")
    await adapter.send_stream_frame("foreground", chat_id="foreground", reply_to="in", turn_id="t")
    result = await adapter.send(
        "background", "done", metadata={"non_conversational": True, "_interim_send": True, "thread_id": "topic"},
    )
    assert result.success
    assert adapter.cards.turns["t"].text == "foreground"
    adapter.cards._client.send_card_to_chat.assert_awaited_once()
    assert adapter.cards._client.send_card_to_chat.call_args.args[0] == "background"
    assert adapter.cards._client.send_card_to_chat.call_args.kwargs["reply_to_message_id"] == "topic"
    await adapter.cards.close()


def test_registry_factory_resolves_credentials_from_host_environment(tmp_path):
    from gateway.platform_registry import platform_registry

    manifest = parse_manifest_file(ROOT / "plugin.yaml", ROOT, "user", "")
    manager = PluginManager()
    try:
        register(PluginContext(manifest, manager))
        config = PlatformConfig(enabled=True)
        assert not config.extra.get("app_id")
        entry = platform_registry.get("feishu")
        assert entry.validate_config(config)
        value = platform_registry.create_adapter("feishu", config)
        assert isinstance(value, FeishuStreamingAdapter)
        ACTIVE.discard(value)
    finally:
        manager.unload()


@pytest.mark.asyncio
async def test_default_cursor_stream_updates_one_answer_element(adapter):
    """真实消费者的默认光标不能导致每帧新建正文元素."""
    set_session_vars(platform="feishu", chat_id="chat", message_id="cursor-in")
    consumer = GatewayStreamConsumer(
        adapter, "chat", config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1),
        initial_reply_to_id="cursor-in",
    )
    sent = asyncio.Queue()

    async def stream_element(card_id, element_id, content, **kwargs):
        await sent.put((element_id, content))

    adapter.cards._client.cardkit_stream_element.side_effect = stream_element
    task = asyncio.create_task(consumer.run())
    try:
        consumer.on_delta("这是一段足够长的流式回答")
        first = await asyncio.wait_for(sent.get(), 2)
        consumer.on_delta("继续")
        second = await asyncio.wait_for(sent.get(), 2)
        assert first[0] == second[0]
        assert first[1] == "这是一段足够长的流式回答"
        assert second[1] == "这是一段足够长的流式回答继续"
    finally:
        consumer.finish("这是一段足够长的流式回答继续")
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_chat", ["chat-a", "chat-b"])
@pytest.mark.parametrize("waiting_for_tool", [False, True])
async def test_long_running_consumer_survives_new_stream(adapter, other_chat, waiting_for_tool):
    """超过旧 TTL 的输出中/工具等待中回合，不能被另一个消费者清理或吞掉最终答案."""
    set_session_vars(platform="feishu", chat_id="chat-a", message_id="in-a", session_key="s-a")
    a = GatewayStreamConsumer(
        adapter, "chat-a", config=StreamConsumerConfig(cursor=""), initial_reply_to_id="in-a",
    )
    tasks = [asyncio.create_task(a.run())]
    try:
        a.on_delta("before")
        assert await asyncio.to_thread(a.flush_pending_sync, 2)
        key, turn = next(iter(adapter.cards.turns.items()))
        turn.session.created_at -= 86400
        if waiting_for_tool:
            await adapter.cards.observe("in-a", "tool_start", {
                "tool_name": "terminal", "tool_call_id": "slow-tool",
            })
        else:
            a.on_delta(" still running")
            assert await asyncio.to_thread(a.flush_pending_sync, 2)

        set_session_vars(platform="feishu", chat_id=other_chat, message_id="in-b", session_key="s-b")
        b = GatewayStreamConsumer(
            adapter, other_chat, config=StreamConsumerConfig(cursor=""), initial_reply_to_id="in-b",
        )
        tasks.append(asyncio.create_task(b.run()))
        b.on_delta("other answer")
        assert await asyncio.to_thread(b.flush_pending_sync, 2)
        assert key in adapter.cards.turns
        adapter.cards._client.cardkit_close_streaming.assert_not_awaited()
        if waiting_for_tool:
            await adapter.cards.observe("in-a", "tool_end", {
                "tool_name": "terminal", "tool_call_id": "slow-tool", "duration_ms": 86400000,
            })
        a.finish("complete long-running answer")
        await asyncio.wait_for(tasks[0], 2)
        assert a.final_content_delivered
        assert "complete long-running answer" in str(adapter.cards._client.cardkit_update.call_args)
        b.finish("other answer")
        await asyncio.wait_for(tasks[1], 2)
        assert b.final_content_delivered
        assert not adapter.cards.turns
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.cards.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "cardkit_batch_update", "cardkit_stream_element", "cardkit_close_streaming", "cardkit_update",
])
async def test_real_consumer_recovers_sequence_conflict_without_fallback(adapter, method):
    from itertools import chain, repeat

    from hermes_lark_streaming.feishu import FeishuAPIError

    operation = getattr(adapter.cards._client, method)
    operation.side_effect = chain([FeishuAPIError("sequence conflict", code=300317)], repeat(None))
    set_session_vars(platform="feishu", chat_id="chat", message_id="in", session_key="s")
    consumer = GatewayStreamConsumer(
        adapter, "chat", config=StreamConsumerConfig(cursor=""), initial_reply_to_id="in",
    )
    task = asyncio.create_task(consumer.run())
    try:
        consumer.on_delta("streamed answer")
        assert await asyncio.to_thread(consumer.flush_pending_sync, 2)
        session = next(iter(adapter.cards.turns.values())).session
        await session.flush.flush_now(lambda: adapter.cards._do_flush(session))
        consumer.finish("complete answer")
        await asyncio.wait_for(task, 2)
        assert consumer.final_content_delivered
        assert "complete answer" in str(adapter.cards._client.cardkit_update.call_args)
        adapter.cards._client.cardkit_create.assert_awaited_once()
        adapter.cards._client.send_card_to_chat.assert_not_called()
        calls = operation.await_args_list
        assert calls[0].kwargs["request_id"] == calls[1].kwargs["request_id"]
        assert calls[0].kwargs["sequence"] + 1 == calls[1].kwargs["sequence"]
        assert not adapter.cards.turns
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.cards.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cardkit_close_streaming", "cardkit_update"])
async def test_exhausted_sequence_recovery_uses_host_fallback(adapter, method):
    from hermes_lark_streaming.feishu import FeishuAPIError

    operation = getattr(adapter.cards._client, method)
    operation.side_effect = FeishuAPIError("sequence conflict", code=300317)
    set_session_vars(platform="feishu", chat_id="chat", message_id="in", session_key="s")
    consumer = GatewayStreamConsumer(
        adapter, "chat", config=StreamConsumerConfig(cursor=""), initial_reply_to_id="in",
    )
    consumer.on_delta("answer")
    consumer.finish("complete answer")
    await asyncio.wait_for(consumer.run(), 2)
    assert operation.await_count == 3
    assert consumer.final_content_delivered
    adapter.cards._client.send_card_to_chat.assert_awaited_once()
    assert "complete answer" in str(adapter.cards._client.send_card_to_chat.call_args)
    assert not adapter.cards.turns
