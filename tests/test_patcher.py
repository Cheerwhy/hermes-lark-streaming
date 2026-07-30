"""Patcher tests — copy real run.py, apply/remove/verify against the copy.

Usage:
    ~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_patcher.py -v
"""

from __future__ import annotations

import ast
import logging
import shutil
import textwrap
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.patcher import (
    MARKERS,
    MK_CRON_DELIVER,
    MK_CRON_DELIVER_END,
    CronPatcher,
    Patcher,
    PatcherError,
    _answer_hook,
    _complete_hook,
    _cron_deliver_hook,
    _followup_complete_hook,
    _remove_block,
    _stop_hook,
    _tool_hook,
)

RUN_SRC = Path.home() / ".hermes" / "hermes-agent" / "gateway" / "run.py"
RUN_BAK = RUN_SRC.with_suffix(RUN_SRC.suffix + ".hermes_lark.bak")
SAMPLES_DIR = Path(__file__).parent / "samples"
SAMPLE_RUN = SAMPLES_DIR / "run.py"

_RUN_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/gateway/run.py"
_CRON_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/cron/scheduler.py"

CRON_SRC = Path.home() / ".hermes" / "hermes-agent" / "cron" / "scheduler.py"
CRON_BAK = CRON_SRC.with_suffix(CRON_SRC.suffix + ".hermes_lark.bak")
SAMPLE_CRON = SAMPLES_DIR / "scheduler.py"

def _ensure_sample() -> Path:
    src = RUN_BAK if RUN_BAK.exists() else RUN_SRC
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, SAMPLE_RUN)
        return SAMPLE_RUN
    # CI fallback: download from GitHub
    try:
        urllib.request.urlretrieve(_RUN_URL, SAMPLE_RUN)
    except Exception as exc:
        pytest.skip(f"run.py not found locally and download failed: {exc}")
    if not SAMPLE_RUN.exists() or SAMPLE_RUN.stat().st_size == 0:
        pytest.skip("run.py download returned empty file")
    return SAMPLE_RUN


@pytest.fixture()
def run_copy(tmp_path: Path) -> Path:
    src = _ensure_sample()
    dst = tmp_path / "run.py"
    shutil.copy2(src, dst)
    return dst


def _ensure_cron_sample() -> Path:
    src = CRON_BAK if CRON_BAK.exists() else CRON_SRC
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.copy2(src, SAMPLE_CRON)
        return SAMPLE_CRON
    try:
        urllib.request.urlretrieve(_CRON_URL, SAMPLE_CRON)
    except Exception as exc:
        pytest.skip(f"scheduler.py not found locally and download failed: {exc}")
    if not SAMPLE_CRON.exists() or SAMPLE_CRON.stat().st_size == 0:
        pytest.skip("scheduler.py download returned empty file")
    return SAMPLE_CRON


@pytest.fixture()
def scheduler_copy(tmp_path: Path) -> Path:
    src = _ensure_cron_sample()
    dst = tmp_path / "scheduler.py"
    shutil.copy2(src, dst)
    return dst


def _patcher(path: Path) -> Patcher:
    return Patcher(run_path=path)


def _cron_patcher(path: Path) -> CronPatcher:
    return CronPatcher(cron_path=path)


def _build_cron_hook_runner():
    namespace: dict = {
        "job": {
            "name": "test",
            "next_run_at": "2026-06-10T14:30:00+08:00",
        }
    }
    source = (
        "def deliver(targets, cleaned_delivery_content, loop, transport=None):\n"
        "    fallback = []\n"
        "    for platform_name, chat_id in targets:\n"
        "        delivered = False\n"
        f"{_cron_deliver_hook('        ')}"
        "        fallback.append(chat_id)\n"
        "    return fallback\n"
    )
    exec(compile(source, "<cron-hook-test>", "exec"), namespace)
    return namespace["deliver"]


def _build_answer_hook_runner(*, use_turn_context: bool):
    namespace: dict = {}
    signature = "text, ctx" if use_turn_context else "text, event_message_id, _run_still_current"
    source = (
        f"def callback({signature}):\n"
        f"{_answer_hook('    ')}"
        "    return 'native'\n"
    )
    exec(compile(source, "<answer-hook-test>", "exec"), namespace)
    return namespace["callback"]


def _build_complete_hook_runner():
    namespace: dict = {}
    source = (
        "async def complete(agent_result, event, response, _response_time, _footer_line):\n"
        f"{_complete_hook('    ')}"
        "    return agent_result, response, _footer_line\n"
    )
    exec(compile(source, "<complete-hook-test>", "exec"), namespace)
    return namespace["complete"]


def _build_followup_complete_hook_runner():
    namespace: dict = {}
    source = (
        "async def complete(event_message_id, result, response):\n"
        f"{_followup_complete_hook('    ')}"
        "    return result, response\n"
    )
    exec(compile(source, "<followup-complete-hook-test>", "exec"), namespace)
    return namespace["complete"]


def _build_stop_hook_runner(key_name: str):
    namespace: dict = {}
    source = f"async def stop(source, {key_name}):\n{_stop_hook('    ')}"
    exec(compile(source, "<stop-hook-test>", "exec"), namespace)
    return namespace["stop"]


def _build_tool_hook_runner(*, use_turn_context: bool):
    namespace: dict = {}
    if use_turn_context:
        source = (
            "class Callbacks:\n"
            "    def __init__(self, ctx):\n"
            "        self._ctx = ctx\n"
            "    def callback(self, event_type, tool_name=None, preview=None):\n"
            f"{_tool_hook('        ')}"
            "        ctx = self._ctx\n"
            "        return 'native'\n"
        )
        exec(compile(source, "<tool-hook-test>", "exec"), namespace)
        return lambda ctx: namespace["Callbacks"](ctx).callback

    source = (
        "def callback(event_type, event_message_id, _run_still_current, "
        "tool_name=None, preview=None):\n"
        f"{_tool_hook('    ')}"
        "    return 'native'\n"
    )
    exec(compile(source, "<tool-hook-test>", "exec"), namespace)
    return lambda _ctx: namespace["callback"]


class TestVerify:
    def test_verify_passes_on_real_run(self, run_copy: Path) -> None:
        _patcher(run_copy).verify_target()

    def test_verify_fails_on_missing_handler(self, tmp_path: Path) -> None:
        p = tmp_path / "run.py"
        p.write_text(
            textwrap.dedent("""\
            async def _stream_delta_cb(text):
                pass
            async def progress_callback(event_type):
                pass
        """)
        )
        with pytest.raises(PatcherError, match="_handle_message_with_agent"):
            _patcher(p).verify_target()

    def test_verify_fails_on_missing_callback(self, tmp_path: Path) -> None:
        p = tmp_path / "run.py"
        p.write_text(
            textwrap.dedent("""\
            async def _handle_message_with_agent(source, event):
                self.hooks.emit("agent:end", {})
        """)
        )
        with pytest.raises(PatcherError, match="Missing injection targets"):
            _patcher(p).verify_target()

    def test_verify_fails_on_missing_reasoning_anchor(self, tmp_path: Path) -> None:
        p = tmp_path / "run.py"
        p.write_text(
            textwrap.dedent("""\
            async def _handle_message_with_agent(source, event):
                self.hooks.emit("agent:end", {})
            async def _stream_delta_cb(text):
                pass
            async def progress_callback(event_type):
                pass
            def _interim_assistant_cb(text):
                pass
            # Restart typing indicator so the user sees activity
            was_interrupted = result.get("interrupted")
            return _preserve_queued_followup_history_offset(result, followup_result)
        """)
        )
        with pytest.raises(PatcherError, match="reasoning_config"):
            _patcher(p).verify_target()

    def test_verify_fails_on_missing_complete_anchor(self, tmp_path: Path) -> None:
        """通过所有其他锚点，但缺 COMPLETE 锚点 `_already_sent = bool(`。"""
        p = tmp_path / "run.py"
        p.write_text(
            textwrap.dedent("""\
            async def _handle_message_with_agent(source, event):
                self.hooks.emit("agent:end", {})
            async def _stream_delta_cb(text):
                pass
            async def progress_callback(event_type):
                pass
            def _interim_assistant_cb(text):
                pass
            # Restart typing indicator so the user sees activity
            was_interrupted = result.get("interrupted")
            return _preserve_queued_followup_history_offset(result, followup_result)
            agent.reasoning_config = reasoning_config
            agent.background_review_callback = _bg_review_send
            images, text_content = adapter.extract_images(response)
        """)
        )
        with pytest.raises(PatcherError, match="complete anchor"):
            _patcher(p).verify_target()

    def test_verify_fails_on_missing_abort_anchor(self, tmp_path: Path) -> None:
        """通过所有其他锚点，但缺 ABORT 锚点 `Discarding stale agent result`。"""
        p = tmp_path / "run.py"
        p.write_text(
            textwrap.dedent("""\
            async def _handle_message_with_agent(source, event):
                self.hooks.emit("agent:end", {})
            async def _stream_delta_cb(text):
                pass
            async def progress_callback(event_type):
                pass
            def _interim_assistant_cb(text):
                pass
            # Restart typing indicator so the user sees activity
            was_interrupted = result.get("interrupted")
            return _preserve_queued_followup_history_offset(result, followup_result)
            agent.reasoning_config = reasoning_config
            agent.background_review_callback = _bg_review_send
            images, text_content = adapter.extract_images(response)
            _already_sent = bool(agent_result.get("already_sent"))
        """)
        )
        with pytest.raises(PatcherError, match="abort anchor"):
            _patcher(p).verify_target()


class TestGeneratedAnswerHook:
    def test_patch_hook_requires_message_id(self) -> None:
        from hermes_lark_streaming.patch import on_answer_delta

        with pytest.raises(TypeError):
            on_answer_delta(text="delta")  # type: ignore[call-arg]

    def test_consumed_delta_still_reaches_streaming_tts(self) -> None:
        namespace: dict = {}
        source = (
            "def callback(text, ctx, _stts_consumer_ref):\n"
            f"{_answer_hook('    ')}"
            "    _stts_consumer_ref.on_delta(text)\n"
        )
        exec(compile(source, "<answer-hook-tts-test>", "exec"), namespace)
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True
        streaming_tts = MagicMock()

        with patch("hermes_lark_streaming.patch.on_answer_delta", return_value=True):
            namespace["callback"]("delta", ctx, streaming_tts)

        streaming_tts.on_delta.assert_called_once_with("delta")

    def test_turn_context_scope_consumes_delta(self) -> None:
        callback = _build_answer_hook_runner(use_turn_context=True)
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True

        with patch(
            "hermes_lark_streaming.patch.on_answer_delta",
            return_value=True,
        ) as on_answer_delta:
            result = callback("delta", ctx)

        assert result is None
        on_answer_delta.assert_called_once_with(message_id="modern-message", text="delta")

    def test_legacy_scope_consumes_delta(self) -> None:
        callback = _build_answer_hook_runner(use_turn_context=False)
        run_still_current = MagicMock(return_value=True)

        with patch(
            "hermes_lark_streaming.patch.on_answer_delta",
            return_value=True,
        ) as on_answer_delta:
            result = callback("delta", "legacy-message", run_still_current)

        assert result is None
        on_answer_delta.assert_called_once_with(message_id="legacy-message", text="delta")

    def test_unconsumed_delta_falls_through_to_native_stream(self) -> None:
        callback = _build_answer_hook_runner(use_turn_context=True)
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True

        with patch("hermes_lark_streaming.patch.on_answer_delta", return_value=False):
            result = callback("delta", ctx)

        assert result == "native"


class TestGeneratedToolHook:
    def test_turn_callbacks_scope_consumes_tool_event_before_ctx_binding(self) -> None:
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True
        ctx.log_queue = None
        callback = _build_tool_hook_runner(use_turn_context=True)(ctx)

        with patch(
            "hermes_lark_streaming.patch.on_tool_updated",
            return_value=True,
        ) as on_tool_updated:
            result = callback("tool.started", tool_name="search", preview="query")

        assert result is None
        on_tool_updated.assert_called_once_with(
            message_id="modern-message",
            tool_name="search",
            status="started",
            detail="query",
        )

    def test_consumed_tool_event_preserves_log_mode(self) -> None:
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True
        ctx.log_queue = MagicMock()
        callback = _build_tool_hook_runner(use_turn_context=True)(ctx)

        with patch("hermes_lark_streaming.patch.on_tool_updated", return_value=True):
            result = callback("tool.started", tool_name="search", preview="query")

        assert result is None
        logged = ctx.log_queue.put.call_args.args[0]
        assert logged.endswith('  search: "query"')

    def test_legacy_scope_consumes_tool_event(self) -> None:
        callback = _build_tool_hook_runner(use_turn_context=False)(None)
        run_still_current = MagicMock(return_value=True)

        with patch(
            "hermes_lark_streaming.patch.on_tool_updated",
            return_value=True,
        ) as on_tool_updated:
            result = callback(
                "tool.completed",
                "legacy-message",
                run_still_current,
                tool_name="search",
                preview="done",
            )

        assert result is None
        on_tool_updated.assert_called_once_with(
            message_id="legacy-message",
            tool_name="search",
            status="completed",
            detail="done",
        )

    def test_exception_is_logged_before_native_fallback(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = MagicMock()
        ctx.event_message_id = "modern-message"
        ctx._run_still_current.return_value = True
        ctx.log_queue = None
        callback = _build_tool_hook_runner(use_turn_context=True)(ctx)

        with (
            caplog.at_level(logging.ERROR, logger="hermes_lark_streaming"),
            patch(
                "hermes_lark_streaming.patch.on_tool_updated",
                side_effect=RuntimeError("tool hook exploded"),
            ),
        ):
            result = callback("tool.started", tool_name="search")

        assert result == "native"
        record = next(record for record in caplog.records if "injected hook failed: tool" in record.message)
        assert record.exc_info is not None


@pytest.mark.parametrize("key_name", ["quick_key", "_quick_key"])
@pytest.mark.asyncio
async def test_generated_stop_hook_uses_available_session_key(key_name: str) -> None:
    stop = _build_stop_hook_runner(key_name)
    source = SimpleNamespace(platform=SimpleNamespace(value="feishu"))

    with patch(
        "hermes_lark_streaming.patch.on_session_aborted",
        new_callable=AsyncMock,
    ) as on_session_aborted:
        await stop(source, "session:chat")

    on_session_aborted.assert_awaited_once_with(session_key="session:chat")


@pytest.mark.asyncio
async def test_generated_complete_hook_keeps_footer_in_card_without_native_resend() -> None:
    complete = _build_complete_hook_runner()
    agent_result: dict = {}
    event = SimpleNamespace(message_id="message")

    with (
        patch(
            "hermes_lark_streaming.patch.on_message_completed_wait",
            new_callable=AsyncMock,
            return_value=True,
        ) as on_completed,
        patch("hermes_lark_streaming.patch.on_message_needs_text_fallback", return_value=False),
    ):
        result, response, footer = await complete(
            agent_result,
            event,
            "answer\n\nruntime footer",
            1.0,
            "runtime footer",
        )

    assert on_completed.await_args.kwargs["answer"] == "answer\n\nruntime footer"
    assert result["already_sent"] is True
    assert response == "answer\n\nruntime footer"
    assert footer == ""


@pytest.mark.asyncio
async def test_generated_complete_hook_suppresses_native_error_after_error_card() -> None:
    complete = _build_complete_hook_runner()
    agent_result = {"failed": True}
    event = SimpleNamespace(message_id="message")

    with (
        patch(
            "hermes_lark_streaming.patch.on_message_completed_wait",
            new_callable=AsyncMock,
            return_value=True,
        ) as on_completed,
        patch("hermes_lark_streaming.patch.on_message_needs_text_fallback", return_value=False),
    ):
        result, response, _footer = await complete(
            agent_result, event, "request failed", 1.0, "",
        )

    assert on_completed.await_args.kwargs["is_error"] is True
    assert result["failed"] is True
    assert response == ""


@pytest.mark.asyncio
async def test_generated_followup_hook_handles_distinct_delivery_result() -> None:
    complete = _build_followup_complete_hook_runner()
    raw_result = {"final_response": "raw answer"}
    delivery_result = {"final_response": "normalized answer"}

    with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
        ctrl = MagicMock()
        ctrl.enabled = True
        ctrl.on_completed_wait = AsyncMock(return_value=True)
        mock_get.return_value = ctrl
        raw_result, delivery_result = await complete("message", raw_result, delivery_result)

    assert ctrl.on_completed_wait.await_args.kwargs["answer"] == "normalized answer"
    assert raw_result["final_response"] == ""
    assert delivery_result["final_response"] == ""


class TestApplyRemove:
    def test_apply_injects_all_markers(self, run_copy: Path) -> None:
        original = run_copy.read_text(encoding="utf-8")
        expected_answer_hooks = sum(
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_stream_delta_cb"
            for node in ast.walk(ast.parse(original))
        )
        patcher = _patcher(run_copy)
        patcher.apply()
        content = run_copy.read_text(encoding="utf-8")
        for begin, end in MARKERS:
            assert begin in content, f"Missing marker: {begin}"
            assert end in content, f"Missing marker: {end}"
        assert content.count("# HERMES_LARK_ANSWER_BEGIN") == expected_answer_hooks
        assert content.count("# HERMES_LARK_ANSWER_END") == expected_answer_hooks

        for begin, end in MARKERS:
            search_from = 0
            while (block_start := content.find(begin, search_from)) != -1:
                block_end = content.index(end, block_start)
                block = content[block_start:block_end]
                assert "injected hook failed:" in block, f"Missing exception log in {begin}"
                assert "except Exception:\n" not in block or "\n    pass" not in block
                search_from = block_end + len(end)

    def test_apply_produces_valid_python(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        content = run_copy.read_text(encoding="utf-8")
        ast.parse(content)  # should not raise

    def test_apply_uses_current_turn_message_id_for_card_session(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        content = run_copy.read_text(encoding="utf-8")

        assert "# HERMES_LARK_NORMALIZE_BEGIN" in content
        assert "source = event.source\n        # HERMES_LARK_NORMALIZE_BEGIN" in content
        assert "on_feishu_normalize(" in content
        assert "on_message_started(" in content
        assert "_lark_anchor_id = self._reply_anchor_for_event(event)" in content
        assert "message_id=event.message_id" in content
        assert "anchor_id=_lark_anchor_id" in content
        assert "session_key=locals().get('session_key') or locals().get('_quick_key')" in content
        assert "_lark_next_message_id = getattr(pending_event, 'message_id', None) or next_message_id" in content
        assert "new_message_id=_lark_next_message_id" in content
        assert "anchor_id=_lark_next_anchor_id" in content
        assert "session_key=locals().get('next_session_key') or locals().get('session_key')" in content
        assert "# HERMES_LARK_FOLLOWUP_COMPLETE_BEGIN" in content
        assert "message_id=event_message_id" in content
        assert "_lark_delivery_result = response if isinstance(response, dict) else result" in content
        assert "message_id=event_message_id, result=_lark_delivery_result" in content
        assert "# HERMES_LARK_FOLLOWUP_RESULT_BEGIN" in content
        assert "on_queued_followup_result(" in content
        assert "on_message_completed_wait(" in content
        assert "on_message_needs_text_fallback" in content
        assert "_lark_card_sent = await on_message_completed_wait(" in content
        assert "agent_result.pop('already_sent', None)" in content
        assert "_lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id" in content
        assert "message_id=_lark_completion_id" in content
        assert "_lark_message_id = ctx.event_message_id" in content
        assert "_lark_run_current = ctx._run_still_current" in content
        assert "on_answer_delta(message_id=_lark_message_id" in content
        assert "on_thinking_delta(message_id=_lark_message_id" in content
        assert "on_reasoning_delta(message_id=_lark_message_id" in content
        assert "on_background_deliver(" in content
        assert "_bg_preview = prompt[:60] + ('...' if len(prompt) > 60 else '')" in content
        assert "content=text_content" in content
        assert "reply_to_message_id=event_message_id" in content
        assert "if not images and not media_files:" in content

        stop_call = content.index('invalidation_reason="stop_command"')
        stop_hook = content.index("# HERMES_LARK_STOP_BEGIN", stop_call)
        stop_return = content.index("return EphemeralReply", stop_call)
        assert stop_call < stop_hook < stop_return
        assert "on_session_aborted" in content[stop_hook:stop_return]
        assert "await on_session_aborted" in content[stop_hook:stop_return]

    def test_apply_idempotent(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        after_first = run_copy.read_text(encoding="utf-8")
        patcher.apply()  # second apply should be no-op
        after_second = run_copy.read_text(encoding="utf-8")
        assert after_first == after_second

    def test_apply_upgrades_partial_patch(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        begin, end = next(pair for pair in MARKERS if "BACKGROUND_REVIEW" in pair[0])
        content = _remove_block(run_copy.read_text(encoding="utf-8"), begin, end)
        run_copy.write_text(content, encoding="utf-8")

        patcher.apply()
        upgraded = run_copy.read_text(encoding="utf-8")

        assert upgraded.count(begin) == 1
        assert upgraded.count(end) == 1

    def test_apply_hard_fails_when_injection_site_missing(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        with (
            patch("hermes_lark_streaming.patcher._find_handler_return", return_value=None),
            pytest.raises(PatcherError, match="locate complete injection site"),
        ):
            patcher.apply()

    def test_remove_restores_markers_free(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        original = run_copy.read_text(encoding="utf-8")
        patcher.apply()
        patcher.remove()
        after_remove = run_copy.read_text(encoding="utf-8")
        assert after_remove == original

    def test_remove_produces_valid_python(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        patcher.remove()
        content = run_copy.read_text(encoding="utf-8")
        ast.parse(content)

    def test_remove_on_unpatched_is_noop(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        original = run_copy.read_text(encoding="utf-8")
        patcher.remove()
        assert run_copy.read_text(encoding="utf-8") == original

    def test_apply_then_remove_repeatedly(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        original = run_copy.read_text(encoding="utf-8")
        for _ in range(3):
            patcher.apply()
            patcher.remove()
        assert run_copy.read_text(encoding="utf-8") == original

    def test_remove_block_leaves_malformed_marker_order_unchanged(self) -> None:
        begin, end = MARKERS[0]
        content = f"before\n{end}\nmiddle\n{begin}\nafter\n"

        assert _remove_block(content, begin, end) == content

    def test_apply_rejects_malformed_existing_markers(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        _, answer_end = next(pair for pair in MARKERS if "ANSWER" in pair[0])
        malformed = run_copy.read_text(encoding="utf-8").replace(answer_end, "", 1)
        run_copy.write_text(malformed, encoding="utf-8")

        with pytest.raises(PatcherError, match="Malformed injected marker"):
            patcher.apply()

        assert run_copy.read_text(encoding="utf-8") == malformed


class TestBackupRestore:
    def test_backup_created_on_apply(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        backup = run_copy.with_suffix(run_copy.suffix + ".hermes_lark.bak")
        assert backup.exists()

    def test_restore_recovers_original(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        original = run_copy.read_text(encoding="utf-8")
        patcher.apply()
        patcher.restore()
        assert run_copy.read_text(encoding="utf-8") == original

    def test_restore_fails_without_backup(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        with pytest.raises(PatcherError, match="No backup found"):
            patcher.restore()


# --- CronPatcher ---


class TestCronVerify:
    def test_verify_passes(self, scheduler_copy: Path) -> None:
        _cron_patcher(scheduler_copy).verify_target()

    def test_verify_fails_missing_delivered_false(self, tmp_path: Path) -> None:
        p = tmp_path / "scheduler.py"
        p.write_text("cleaned_delivery_content = ''\n")
        with pytest.raises(PatcherError, match="delivered = False"):
            _cron_patcher(p).verify_target()

    def test_verify_fails_missing_cleaned_content(self, tmp_path: Path) -> None:
        p = tmp_path / "scheduler.py"
        p.write_text("    delivered = False\n")
        with pytest.raises(PatcherError, match="cleaned_delivery_content"):
            _cron_patcher(p).verify_target()


class TestCronApplyRemove:
    def test_apply_injects_markers(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = scheduler_copy.read_text(encoding="utf-8")
        assert MK_CRON_DELIVER in content
        assert MK_CRON_DELIVER_END in content

    def test_apply_produces_valid_python(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = scheduler_copy.read_text(encoding="utf-8")
        compile(content, str(scheduler_copy), "exec")

    def test_apply_idempotent(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        first = scheduler_copy.read_text(encoding="utf-8")
        cp.apply()
        assert scheduler_copy.read_text(encoding="utf-8") == first

    def test_apply_normalizes_duplicate_marker_blocks(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = scheduler_copy.read_text(encoding="utf-8")
        begin = content.index(MK_CRON_DELIVER)
        end = content.index(MK_CRON_DELIVER_END, begin) + len(MK_CRON_DELIVER_END)
        end = content.find("\n", end) + 1
        block = content[begin:end]
        scheduler_copy.write_text(content[:begin] + block + block + content[end:], encoding="utf-8")

        cp.apply()

        normalized = scheduler_copy.read_text(encoding="utf-8")
        assert normalized.count(MK_CRON_DELIVER) == 1
        assert normalized.count(MK_CRON_DELIVER_END) == 1

    def test_remove_restores_original(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        original = scheduler_copy.read_text(encoding="utf-8")
        cp.apply()
        cp.remove()
        assert scheduler_copy.read_text(encoding="utf-8") == original

    def test_remove_on_unpatched_is_noop(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        original = scheduler_copy.read_text(encoding="utf-8")
        cp.remove()
        assert scheduler_copy.read_text(encoding="utf-8") == original

    def test_injected_hook_references_on_cron_deliver(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = scheduler_copy.read_text(encoding="utf-8")
        assert "on_cron_deliver" in content
        assert "platform_name.lower()" in content
        assert "is_relay" in content
        assert "injected hook failed: cron_deliver" in content
        assert "delivered = True" in content

    def test_injected_hook_skips_duplicate_card_target(self) -> None:
        deliver = _build_cron_hook_runner()
        sent = []

        def fake_on_cron_deliver(
            *, chat_id, content, loop, task_name, run_time
        ):
            sent.append((chat_id, content, task_name, run_time))
            return True

        targets = [("feishu", "oc_same"), ("feishu", "oc_same")]
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=fake_on_cron_deliver,
        ):
            fallback = deliver(targets, " failed ", object())

        assert sent == [
            (
                "oc_same",
                "failed",
                "test",
                "2026-06-10T14:30:00+08:00",
            )
        ]
        assert fallback == []

    def test_injected_hook_retries_duplicate_target_after_failure(self) -> None:
        deliver = _build_cron_hook_runner()
        targets = [("feishu", "oc_same"), ("feishu", "oc_same")]
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=[False, True],
        ) as mock_deliver:
            fallback = deliver(targets, "failed", object())

        assert mock_deliver.call_count == 2
        assert fallback == ["oc_same"]

    def test_injected_hook_skips_relay_transport(self) -> None:
        deliver = _build_cron_hook_runner()
        transport = MagicMock()
        transport.is_relay = True

        with patch("hermes_lark_streaming.patch.on_cron_deliver") as mock_deliver:
            fallback = deliver([("feishu", "oc_relay")], "report", None, transport=transport)

        assert fallback == ["oc_relay"]
        mock_deliver.assert_not_called()


class TestCronBackupRestore:
    def test_backup_created_on_apply(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        backup = scheduler_copy.with_suffix(scheduler_copy.suffix + ".hermes_lark.bak")
        assert backup.exists()

    def test_restore_recovers_original(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        original = scheduler_copy.read_text(encoding="utf-8")
        cp.apply()
        cp.restore()
        assert scheduler_copy.read_text(encoding="utf-8") == original

    def test_restore_fails_without_backup(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        with pytest.raises(PatcherError, match="No backup found"):
            cp.restore()


class TestOnCronDeliverHook:
    def test_returns_false_when_disabled(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = False
            mock_get.return_value = ctrl
            assert on_cron_deliver(chat_id="c1", content="text", loop=MagicMock()) is False

    def test_delegates_to_controller_when_no_loop(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_cron_deliver.return_value = True
            mock_get.return_value = ctrl
            assert on_cron_deliver(chat_id="c1", content="text", loop=None) is True
            ctrl.on_cron_deliver.assert_called_once_with(
                chat_id="c1", content="text", loop=None,
                task_name="", run_time="",
            )

    def test_delegates_to_controller(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        loop = MagicMock()
        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_cron_deliver.return_value = True
            mock_get.return_value = ctrl
            result = on_cron_deliver(chat_id="c1", content="hello", loop=loop)
            assert result is True
            ctrl.on_cron_deliver.assert_called_once_with(
                chat_id="c1", content="hello", loop=loop,
                task_name="", run_time="",
            )


class TestQueuedFollowupHooks:
    @pytest.mark.asyncio
    async def test_boundary_marks_result_when_card_sent(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_boundary

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_completed_wait = AsyncMock(return_value=True)
            mock_get.return_value = ctrl
            result = {"final_response": "ok", "model": "m"}

            assert await on_queued_followup_boundary(message_id="msg", result=result) is True

            assert result["response_previewed"] is True
            assert result["already_sent"] is True
            assert result["final_response"] == ""

    @pytest.mark.asyncio
    async def test_boundary_consumes_fallback_when_card_not_sent(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_boundary

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            ctrl.on_completed_wait = AsyncMock(return_value=False)
            mock_get.return_value = ctrl
            result = {"final_response": "plain"}

            assert await on_queued_followup_boundary(message_id="msg", result=result) is False

            ctrl.consume_text_fallback.assert_called_once_with("msg")
            assert "response_previewed" not in result

    def test_result_hook_preserves_deepest_completion_id(self) -> None:
        from hermes_lark_streaming.patch import on_queued_followup_result

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            mock_get.return_value = ctrl
            result = {"_hermes_lark_completion_id": "deep"}

            on_queued_followup_result(message_id="outer", followup_result=result)

            assert result["_hermes_lark_completion_id"] == "deep"
