"""Patcher tests — copy real gateway modules, apply/remove/verify against the copies.

0.21 multi-file layout: run_inbound / run_turn / run_turn_runner / run_busy (+ scheduler_delivery).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    _default_cron_path,
    _default_module_paths,
    _followup_complete_hook,
    _remove_block,
    _stop_hook,
    _tool_hook,
)

_STEMS = ("run_inbound", "run_turn", "run_turn_runner", "run_busy")


@pytest.fixture()
def mod_paths(tmp_path: Path) -> dict[str, Path]:
    """4 个 gateway 模块的原始副本（优先取 install 前的 .bak，避免把已注入块当原始代码）."""
    real = _default_module_paths()
    out: dict[str, Path] = {}
    for stem in _STEMS:
        src = real[stem]
        bak = src.with_suffix(src.suffix + ".hermes_lark.bak")
        dst = tmp_path / f"{stem}.py"
        shutil.copy2(bak if bak.exists() else src, dst)
        out[stem] = dst
    return out


@pytest.fixture()
def run_copy(mod_paths: dict[str, Path]) -> dict[str, Path]:
    return mod_paths


@pytest.fixture()
def scheduler_copy(tmp_path: Path) -> Path:
    src = _default_cron_path()
    bak = src.with_suffix(src.suffix + ".hermes_lark.bak")
    dst = tmp_path / "scheduler_delivery.py"
    shutil.copy2(bak if bak.exists() else src, dst)
    return dst


def _patcher(paths) -> Patcher:
    if isinstance(paths, Path):
        paths = _default_module_paths() | {"run_turn": paths}
    return Patcher(module_paths=paths)


def _cron_patcher(path: Path) -> CronPatcher:
    return CronPatcher(cron_path=path)


def _all_content(paths: dict[str, Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths.values())


class TestVerify:
    def test_verify_passes_on_real_modules(self, run_copy: dict) -> None:
        _patcher(run_copy).verify_target()

    def test_verify_fails_on_missing_handler(self, mod_paths: dict) -> None:
        content = mod_paths["run_turn"].read_text(encoding="utf-8")
        content = content.replace("async def _handle_message_with_agent", "async def _renamed_handler")
        mod_paths["run_turn"].write_text(content, encoding="utf-8")
        with pytest.raises(PatcherError, match="_handle_message_with_agent"):
            _patcher(mod_paths).verify_target()

    def test_verify_fails_on_missing_anchor(self, mod_paths: dict) -> None:
        content = mod_paths["run_turn"].read_text(encoding="utf-8")
        content = content.replace("# Restart the typing indicator", "# typing")
        mod_paths["run_turn"].write_text(content, encoding="utf-8")
        with pytest.raises(PatcherError, match="interrupt site"):
            _patcher(mod_paths).verify_target()


def _build_cron_hook_runner():
    namespace: dict = {
        "job": {"name": "test", "next_run_at": "2026-06-10T14:30:00+08:00"},
    }
    source = (
        "def deliver(targets, cleaned_delivery_content, loop):\n"
        "    fallback = []\n"
        "    for t in targets:\n"
        f"{_cron_deliver_hook('        ')}"
        "        fallback.append(t.chat_id)\n"
        "    return fallback\n"
    )
    exec(compile(source, "<cron-hook-test>", "exec"), namespace)
    return namespace["deliver"]


def _build_complete_hook_runner():
    namespace: dict = {}
    source = (
        "async def complete(agent_result, event, response, _turn_seconds, _footer_line):\n"
        f"{_complete_hook('    ')}"
        "    return agent_result, response, _footer_line\n"
    )
    exec(compile(source, "<complete-hook-test>", "exec"), namespace)
    return namespace["complete"]


def _build_followup_complete_hook_runner():
    namespace: dict = {}
    source = (
        "async def complete(turn_ctx, response, result):\n"
        f"{_followup_complete_hook('    ')}"
        "    return result\n"
    )
    exec(compile(source, "<followup-complete-hook-test>", "exec"), namespace)
    return namespace["complete"]


def _build_stop_hook_runner(key_name: str):
    namespace: dict = {}
    source = f"async def stop(source, {key_name}):\n{_stop_hook('    ')}"
    exec(compile(source, "<stop-hook-test>", "exec"), namespace)
    return namespace["stop"]


def _build_tool_hook_runner():
    namespace: dict = {}
    source = (
        "class Callbacks:\n"
        "    def __init__(self, ctx):\n"
        "        self._ctx = ctx\n"
        "    def callback(self, event_type, tool_name=None, preview=None):\n"
        f"{_tool_hook('        ')}"
        "        return 'native'\n"
    )
    exec(compile(source, "<tool-hook-test>", "exec"), namespace)
    return lambda ctx: namespace["Callbacks"](ctx).callback


def _build_answer_hook_runner():
    namespace: dict = {}
    source = (
        "def callback(text, ctx):\n"
        f"{_answer_hook('    ')}"
        "    return 'native'\n"
    )
    exec(compile(source, "<answer-hook-test>", "exec"), namespace)
    return namespace["callback"]


class TestHookBodies:
    def test_generated_complete_hook_keeps_footer_in_card_without_native_resend(self) -> None:
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
            result, response, footer = asyncio.run(
                complete(agent_result, event, "answer\nruntime footer", 1.0, "runtime footer")
            )

        assert on_completed.await_args.kwargs["answer"] == "answer\nruntime footer"
        assert result["already_sent"] is True
        assert response == "answer\nruntime footer"
        assert footer == ""

    def test_generated_complete_hook_suppresses_native_error_after_error_card(self) -> None:
        complete = _build_complete_hook_runner()
        with (
            patch(
                "hermes_lark_streaming.patch.on_message_completed_wait",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("hermes_lark_streaming.patch.on_message_needs_text_fallback", return_value=False),
        ):
            result, response, _footer = asyncio.run(
                complete({"failed": True}, SimpleNamespace(message_id="m"), "request failed", 1.0, "")
            )
        assert result["failed"] is True
        assert response == ""

    def test_generated_answer_hook_consumes_delta_and_teeds_tts(self) -> None:
        class FakeSTTS:
            def __init__(self):
                self.got: list[str] = []

            def on_delta(self, text: str) -> None:
                self.got.append(text)

        stts = FakeSTTS()
        ctx = SimpleNamespace(
            event_message_id="om_1", _run_still_current=lambda: True,
            streaming_tts_consumer_holder=[stts],
        )
        callback = _build_answer_hook_runner()
        with patch("hermes_lark_streaming.patch.on_answer_delta", return_value=True):
            assert callback("hello", ctx) is None
        assert stts.got == ["hello"]

    def test_generated_answer_hook_falls_through_when_not_consumed(self) -> None:
        ctx = SimpleNamespace(
            event_message_id="om_1", _run_still_current=lambda: True,
            streaming_tts_consumer_holder=[None],
        )
        callback = _build_answer_hook_runner()
        with patch("hermes_lark_streaming.patch.on_answer_delta", return_value=False):
            assert callback("hello", ctx) == "native"

    def test_generated_tool_hook_consumes_event(self) -> None:
        ctx = SimpleNamespace(event_message_id="om_2", _run_still_current=lambda: True, log_queue=None)
        callback = _build_tool_hook_runner()(ctx)
        with patch("hermes_lark_streaming.patch.on_tool_updated", return_value=True) as m:
            assert callback("tool.started", tool_name="search", preview="q") is None
        assert m.called

    def test_generated_tool_hook_is_inert_without_ctx(self) -> None:
        callback = _build_tool_hook_runner()(None)
        with patch("hermes_lark_streaming.patch.on_tool_updated") as m:
            assert callback("tool.started", tool_name="search") == "native"
        assert not m.called

    @pytest.mark.parametrize("key_name", ["quick_key", "_quick_key"])
    def test_generated_stop_hook_uses_available_session_key(self, key_name: str) -> None:
        stop = _build_stop_hook_runner(key_name)
        source = SimpleNamespace(platform=SimpleNamespace(value="feishu"))
        with patch(
            "hermes_lark_streaming.patch.on_session_aborted",
            new_callable=AsyncMock,
        ) as on_session_aborted:
            asyncio.run(stop(source, "session:chat"))
        on_session_aborted.assert_awaited_once_with(session_key="session:chat")

    def test_generated_followup_hook_sets_result_flags(self) -> None:
        complete = _build_followup_complete_hook_runner()
        raw_result = {"final_response": "raw answer"}
        delivery_result = {"final_response": "normalized answer"}
        turn_ctx = SimpleNamespace(result_holder=[raw_result], event_message_id="om_3")
        with patch(
            "hermes_lark_streaming.patch.on_queued_followup_boundary",
            new_callable=AsyncMock,
            return_value=True,
        ) as boundary:
            out = asyncio.run(complete(turn_ctx, delivery_result, raw_result))
        assert boundary.await_args.kwargs["message_id"] == "om_3"
        assert boundary.await_args.kwargs["result"] is delivery_result
        assert out["response_previewed"] is True
        assert out["already_sent"] is True

    def test_generated_cron_hook_skips_duplicate_card_target(self) -> None:
        deliver = _build_cron_hook_runner()
        sent: list[tuple] = []

        def fake_on_cron_deliver(*, chat_id, content, loop, task_name, run_time):
            sent.append((chat_id, content, task_name, run_time))
            return True

        t = SimpleNamespace(platform_name="feishu", chat_id="oc_same", is_relay=False)
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=fake_on_cron_deliver,
        ):
            fallback = deliver([t, t], " failed ", object())
        assert sent == [("oc_same", "failed", "test", "2026-06-10T14:30:00+08:00")]
        assert fallback == []

    def test_generated_cron_hook_skips_relay_transport(self) -> None:
        deliver = _build_cron_hook_runner()
        t = SimpleNamespace(platform_name="feishu", chat_id="oc_relay", is_relay=True)
        with patch("hermes_lark_streaming.patch.on_cron_deliver") as mock_deliver:
            fallback = deliver([t], "report", None)
        assert fallback == ["oc_relay"]
        mock_deliver.assert_not_called()

    def test_exception_is_logged_before_native_fallback(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = SimpleNamespace(event_message_id="om_9", _run_still_current=lambda: True)
        callback = _build_answer_hook_runner()
        with (
            caplog.at_level(__import__("logging").ERROR, logger="hermes_lark_streaming"),
            patch(
                "hermes_lark_streaming.patch.on_answer_delta",
                side_effect=RuntimeError("answer hook exploded"),
            ),
        ):
            assert callback("hi", ctx) == "native"
        assert any("injected hook failed: answer" in r.message for r in caplog.records)


class TestApplyRemove:
    def test_apply_injects_all_markers(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        content = _all_content(run_copy)
        for begin, end in MARKERS:
            assert begin in content, f"Missing marker: {begin}"
            assert end in content, f"Missing marker: {end}"

        for begin, end in MARKERS:
            search_from = 0
            while (block_start := content.find(begin, search_from)) != -1:
                block_end = content.index(end, block_start)
                block = content[block_start:block_end]
                assert "injected hook failed:" in block, f"Missing exception log in {begin}"
                search_from = block_end + len(end)

    def test_apply_produces_valid_python(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        for path in run_copy.values():
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_apply_uses_current_turn_message_id_for_card_session(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        inbound = run_copy["run_inbound"].read_text(encoding="utf-8")
        turn = run_copy["run_turn"].read_text(encoding="utf-8")
        runner = run_copy["run_turn_runner"].read_text(encoding="utf-8")
        busy = run_copy["run_busy"].read_text(encoding="utf-8")

        assert "# HERMES_LARK_NORMALIZE_BEGIN" in inbound
        assert "on_feishu_normalize(" in inbound
        assert "on_message_started(" in turn
        assert "_lark_anchor_id = self._reply_anchor_for_event(event)" in turn
        assert "message_id=event.message_id" in turn
        assert "anchor_id=_lark_anchor_id" in turn
        assert "_lark_next_message_id = getattr(pending_event, 'message_id', None) or next_message_id" in turn
        assert "new_message_id=_lark_next_message_id" in turn
        assert "on_message_completed_wait(" in turn
        assert "_lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id" in turn
        assert "duration=_turn_seconds" in turn
        assert "on_queued_followup_boundary(" in turn
        assert "message_id=turn_ctx.event_message_id, result=_lark_delivery_result" in turn
        assert "on_queued_followup_result(" in turn
        assert "on_background_deliver(" in turn
        assert "_bg_preview = prompt[:60] + ('...' if len(prompt) > 60 else '')" in turn
        assert "reply_to_message_id=event_message_id" in turn
        assert "_lark_message_id = ctx.event_message_id" in runner
        assert "_lark_run_current = ctx._run_still_current" in runner
        assert "on_answer_delta(message_id=_lark_message_id" in runner
        assert "on_thinking_delta(message_id=_lark_message_id" in runner
        assert "on_reasoning_delta(message_id=_lark_message_id" in runner
        assert "on_tool_updated(" in runner
        assert "agent.clarify_callback = _lark_clarify_wrapper" in runner
        assert "on_session_aborted" in busy
        assert "await on_session_aborted" in busy

        stop_call = busy.index('invalidation_reason="stop_command"')
        stop_hook = busy.index("# HERMES_LARK_STOP_BEGIN", stop_call)
        stop_return = busy.index("return EphemeralReply", stop_call)
        assert stop_call < stop_hook < stop_return

    def test_apply_idempotent(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        after_first = {s: p.read_bytes() for s, p in run_copy.items()}
        patcher.apply()  # second apply should be no-op
        for stem, data in after_first.items():
            assert run_copy[stem].read_bytes() == data

    def test_apply_upgrades_partial_patch(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        begin, end = next(pair for pair in MARKERS if "BACKGROUND_REVIEW" in pair[0])
        content = run_copy["run_turn_runner"].read_text(encoding="utf-8")
        content = _remove_block(content, begin, end)
        run_copy["run_turn_runner"].write_text(content, encoding="utf-8")

        patcher.apply()
        upgraded = run_copy["run_turn_runner"].read_text(encoding="utf-8")
        assert upgraded.count(begin) == 1
        assert upgraded.count(end) == 1

    def test_apply_hard_fails_when_injection_site_missing(self, run_copy: dict) -> None:
        content = run_copy["run_turn"].read_text(encoding="utf-8")
        content = content.replace("# Restart the typing indicator", "# gone")
        run_copy["run_turn"].write_text(content, encoding="utf-8")
        with pytest.raises(PatcherError, match="interrupt site anchor"):
            _patcher(run_copy).apply()

    def test_remove_restores_markers_free(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        original = {s: p.read_bytes() for s, p in run_copy.items()}
        patcher.apply()
        patcher.remove()
        for stem, data in original.items():
            assert run_copy[stem].read_bytes() == data

    def test_remove_on_unpatched_is_noop(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        original = {s: p.read_bytes() for s, p in run_copy.items()}
        patcher.remove()
        for stem, data in original.items():
            assert run_copy[stem].read_bytes() == data

    def test_apply_then_remove_repeatedly(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        original = {s: p.read_bytes() for s, p in run_copy.items()}
        for _ in range(3):
            patcher.apply()
            patcher.remove()
        for stem, data in original.items():
            assert run_copy[stem].read_bytes() == data

    def test_remove_block_leaves_malformed_marker_order_unchanged(self) -> None:
        begin, end = MARKERS[0]
        content = f"before\n{end}\nmiddle\n{begin}\nafter\n"
        assert _remove_block(content, begin, end) == content

    def test_apply_rejects_malformed_existing_markers(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        _, answer_end = next(pair for pair in MARKERS if "ANSWER" in pair[0])
        malformed = run_copy["run_turn_runner"].read_text(encoding="utf-8").replace(answer_end, "", 1)
        run_copy["run_turn_runner"].write_text(malformed, encoding="utf-8")

        with pytest.raises(PatcherError, match="Malformed injected marker"):
            patcher.apply()


class TestBackupRestore:
    def test_backup_created_on_apply(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        for stem, path in run_copy.items():
            backup = path.with_suffix(path.suffix + ".hermes_lark.bak")
            assert backup.exists(), f"No backup for {stem}"

    def test_restore_recovers_original(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        original = {s: p.read_bytes() for s, p in run_copy.items()}
        patcher.apply()
        patcher.restore()
        for stem, data in original.items():
            assert run_copy[stem].read_bytes() == data

    def test_restore_fails_without_backup(self, run_copy: dict) -> None:
        patcher = _patcher(run_copy)
        with pytest.raises(PatcherError, match="No backup found"):
            patcher.restore()


class TestCronVerify:
    def test_verify_passes(self, scheduler_copy: Path) -> None:
        _cron_patcher(scheduler_copy).verify_target()

    def test_verify_fails_missing_anchor(self, tmp_path: Path) -> None:
        p = tmp_path / "scheduler_delivery.py"
        p.write_text("cleaned_delivery_content = ''\n")
        with pytest.raises(PatcherError, match="target_errors"):
            _cron_patcher(p).verify_target()

    def test_verify_fails_missing_cleaned_content(self, tmp_path: Path) -> None:
        p = tmp_path / "scheduler_delivery.py"
        p.write_text("target_errors: list = []\n")
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
        original = scheduler_copy.read_bytes()
        cp.apply()
        cp.remove()
        assert scheduler_copy.read_bytes() == original

    def test_remove_on_unpatched_is_noop(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        original = scheduler_copy.read_bytes()
        cp.remove()
        assert scheduler_copy.read_bytes() == original

    def test_injected_hook_references_on_cron_deliver(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = scheduler_copy.read_text(encoding="utf-8")
        assert "on_cron_deliver" in content
        assert "t.platform_name.lower()" in content
        assert "t.is_relay" in content
        assert "injected hook failed: cron_deliver" in content
        assert "delivered = True" in content

    def test_backup_created_on_apply(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        backup = scheduler_copy.with_suffix(scheduler_copy.suffix + ".hermes_lark.bak")
        assert backup.exists()

    def test_restore_recovers_original(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        original = scheduler_copy.read_bytes()
        cp.apply()
        cp.restore()
        assert scheduler_copy.read_bytes() == original
