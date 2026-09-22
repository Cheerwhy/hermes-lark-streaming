"""Patcher tests — copy real Hermes sources, apply/remove/verify against the copy.

Targets the split gateway layout only (Hermes >= 0.21.1); the monolithic
injection path was removed, so no test materializes a single-file run.py.

Usage:
    ~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_patcher.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_lark_streaming.patcher import (
    MARKERS,
    MK_CRON_DELIVER,
    MK_CRON_DELIVER_END,
    CronPatcher,
    Patcher,
    PatcherError,
    _complete_body,
    _remove_block,
    _stop_hook,
)


@pytest.fixture()
def run_copy(hermes_tree: Path) -> Path:
    return hermes_tree / "gateway" / "run.py"


@pytest.fixture()
def scheduler_copy(hermes_tree: Path) -> Path:
    return hermes_tree / "cron" / "scheduler.py"


def _patcher(path: Path) -> Patcher:
    return Patcher(run_path=path)


def _cron_patcher(path: Path) -> CronPatcher:
    return CronPatcher(cron_path=path)


def _build_complete_body_runner():
    namespace: dict = {}
    source = (
        "async def complete(agent_result, event, response, _response_time, _footer_line):\n"
        "    _lark_completion_answer = _lark_original_response = response\n"
        "    _turn_seconds = _response_time\n"
        f"{_complete_body('    ')}"
        "    return agent_result, response, _footer_line\n"
    )
    exec(compile(source, "<complete-hook-test>", "exec"), namespace)
    return namespace["complete"]


def _build_stop_hook_runner(key_name: str):
    namespace: dict = {}
    source = f"async def stop(source, {key_name}):\n{_stop_hook('    ')}"
    exec(compile(source, "<stop-hook-test>", "exec"), namespace)
    return namespace["stop"]


class TestGeneratedAnswerHook:
    def test_patch_hook_requires_message_id(self) -> None:
        from hermes_lark_streaming.patch import on_answer_delta

        with pytest.raises(TypeError):
            on_answer_delta(text="delta")  # type: ignore[call-arg]


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
async def test_generated_complete_body_keeps_footer_in_card_without_native_resend() -> None:
    complete = _build_complete_body_runner()
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
async def test_generated_complete_body_suppresses_native_error_after_error_card() -> None:
    complete = _build_complete_body_runner()
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


class TestApplyRemove:
    #: Where each hook is expected to land in the split gateway layout.
    #: THINKING has no standalone block here: run_turn_runner.py inlines the
    #: interim callback into the ANSWER hook.
    SPLIT_MARKER_FILES: ClassVar[dict[str, list[str]]] = {
        "run_inbound.py": ["NORMALIZE", "ABORT"],
        "run_turn.py": [
            "START", "COMPLETE", "FOLLOWUP_COMPLETE", "FOLLOWUP_RESULT",
            "ABORT", "INTERRUPT", "BG_DELIVER",
        ],
        "run_turn_runner.py": ["TOOL", "ANSWER", "REASONING", "BACKGROUND_REVIEW", "CLARIFY"],
        "run_busy.py": ["STOP"],
    }

    def _patched_sources(self, run_copy: Path) -> dict[str, str]:
        patcher = _patcher(run_copy)
        patcher.apply()
        return {path.name: path.read_text(encoding="utf-8") for path in patcher.target_paths}

    def test_apply_injects_all_markers(self, run_copy: Path) -> None:
        hermes_tree = run_copy.parent.parent
        expected_answer_hooks = sum(
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "stream_delta_cb"
            for node in ast.walk(ast.parse((hermes_tree / "gateway/run_turn_runner.py").read_text(
                encoding="utf-8")))
        )
        original_entrypoint = run_copy.read_text(encoding="utf-8")
        sources = self._patched_sources(run_copy)

        # run.py itself stays untouched; hooks live in the split modules.
        assert sources["run.py"] == original_entrypoint

        for filename, labels in self.SPLIT_MARKER_FILES.items():
            content = sources[filename]
            for label in labels:
                begin = f"# HERMES_LARK_{label}_BEGIN"
                end = f"# HERMES_LARK_{label}_END"
                assert begin in content, f"Missing marker in {filename}: {begin}"
                assert end in content, f"Missing marker in {filename}: {end}"

                # Repeated pairs are intentional: every injected block that performs
                # plugin work must carry its own exception guard. COMPLETE is the one
                # exception — its first block is a bare wrapper around the guarded body.
                blocks = []
                search_from = 0
                while (block_start := content.find(begin, search_from)) != -1:
                    block_end = content.index(end, block_start)
                    blocks.append(content[block_start:block_end])
                    search_from = block_end + len(end)
                assert any("injected hook failed:" in block for block in blocks), (
                    f"Missing exception log in {filename}: {begin}"
                )
                for block in blocks:
                    assert "except Exception:\n" not in block or "\n    pass" not in block

        assert sources["run_turn_runner.py"].count("# HERMES_LARK_ANSWER_BEGIN") == expected_answer_hooks
        assert sources["run_turn_runner.py"].count("# HERMES_LARK_ANSWER_END") == expected_answer_hooks

    def test_apply_uses_current_turn_message_id_for_card_session(self, run_copy: Path) -> None:
        sources = self._patched_sources(run_copy)
        inbound = sources["run_inbound.py"]
        turn = sources["run_turn.py"]
        runner = sources["run_turn_runner.py"]
        busy = sources["run_busy.py"]

        # Normalize runs on both admission sites, right after source is bound.
        assert "# HERMES_LARK_NORMALIZE_BEGIN" in inbound
        assert "source = event.source\n        # HERMES_LARK_NORMALIZE_BEGIN" in inbound
        assert "on_feishu_normalize(" in inbound

        # Start resolves the reply anchor separately from the session identity.
        assert "on_message_started(" in turn
        assert "_lark_anchor_id = self._reply_anchor_for_event(event)" in turn
        assert "message_id=event.message_id" in turn
        assert "anchor_id=_lark_anchor_id" in turn
        assert "session_key=locals().get('session_key') or locals().get('_quick_key')" in turn

        # Interrupt redirects the card to the pending follow-up identity.
        assert "# HERMES_LARK_INTERRUPT_BEGIN" in turn
        assert "_lark_next_id = getattr(pending_event, 'message_id', None) or next_message_id" in turn
        assert "new_message_id=_lark_next_id" in turn
        assert "anchor_id=next_message_id" in turn
        assert "session_key=next_session_key" in turn

        # Follow-up boundary/result hooks carry the deepest completion id upward.
        assert "# HERMES_LARK_FOLLOWUP_COMPLETE_BEGIN" in turn
        assert "_lark_id = _delivery_result.get('_hermes_lark_completion_id') or " in turn
        assert "on_message_completed_wait(" in turn
        assert "# HERMES_LARK_FOLLOWUP_RESULT_BEGIN" in turn
        assert "on_queued_followup_result(" in turn

        # Completion strips only the footer the plugin itself observed.
        assert "# HERMES_LARK_COMPLETE_BEGIN" in turn
        assert "on_message_needs_text_fallback" in turn
        assert "_lark_completion_answer = response" in turn

        # Delta hooks use the turn context identity.
        assert "in _lark_ctrl._sessions" in runner
        assert "on_answer_delta(message_id=" in runner
        assert "on_thinking_delta(message_id=" in runner
        assert "on_tool_updated(message_id=" in runner
        assert "on_reasoning_delta(message_id=" in runner

        # Background delivery keeps its reply anchor and content passthrough.
        assert "on_background_deliver(" in turn
        assert "content=text_content" in turn
        assert "reply_to_message_id=" in turn

        # /stop aborts the busy session before the native ephemeral reply.
        stop_call = busy.index('invalidation_reason="stop_command"')
        stop_hook = busy.index("# HERMES_LARK_STOP_BEGIN", stop_call)
        stop_return = busy.index("return EphemeralReply", stop_call)
        assert stop_call < stop_hook < stop_return
        assert "await on_session_aborted" in busy[stop_hook:stop_return]

    def test_remove_block_leaves_malformed_marker_order_unchanged(self) -> None:
        begin, end = MARKERS[0]
        content = f"before\n{end}\nmiddle\n{begin}\nafter\n"

        assert _remove_block(content, begin, end) == content


class TestBackupRestore:
    def test_backup_created_on_apply(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        # run.py is never injected, so it gets no backup; the split modules do.
        assert not run_copy.with_suffix(run_copy.suffix + ".hermes_lark.bak").exists()
        for path in patcher.target_paths[1:]:
            assert path.with_suffix(path.suffix + ".hermes_lark.bak").exists()

    def test_restore_fails_without_backup(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        with pytest.raises(PatcherError, match="No backup found"):
            patcher.restore()


# --- CronPatcher ---


class TestCronVerify:

    def test_verify_fails_without_split_delivery_module(self, tmp_path: Path) -> None:
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        p = cron_dir / "scheduler.py"
        p.write_text("def tick():\n    pass\n")
        with pytest.raises(PatcherError, match="Missing split cron module"):
            _cron_patcher(p)


class TestCronApplyRemove:
    def _cron_target(self, scheduler_copy: Path) -> Path:
        """CronPatcher redirects injection to the split scheduler_delivery.py."""
        return scheduler_copy.with_name("scheduler_delivery.py")

    def test_apply_injects_markers(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = self._cron_target(scheduler_copy).read_text(encoding="utf-8")
        assert MK_CRON_DELIVER in content
        assert MK_CRON_DELIVER_END in content

    def test_apply_normalizes_duplicate_marker_blocks(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        target = self._cron_target(scheduler_copy)
        content = target.read_text(encoding="utf-8")
        # Two blocks are intentional: one per split delivery lane (live + standalone).
        blocks_expected = content.count(MK_CRON_DELIVER)
        assert blocks_expected == 2
        begin = content.index(MK_CRON_DELIVER)
        end = content.index(MK_CRON_DELIVER_END, begin) + len(MK_CRON_DELIVER_END)
        end = content.find("\n", end) + 1
        block = content[begin:end]
        target.write_text(content[:begin] + block + block + content[end:], encoding="utf-8")

        cp.apply()

        normalized = target.read_text(encoding="utf-8")
        assert normalized.count(MK_CRON_DELIVER) == blocks_expected
        assert normalized.count(MK_CRON_DELIVER_END) == blocks_expected

    def test_injected_hook_references_on_cron_deliver(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = self._cron_target(scheduler_copy).read_text(encoding="utf-8")
        # The split lanes call the injector helper, which owns the on_cron_deliver call.
        assert content.count("from hermes_lark_streaming.split_cron import _try_cron_card") == 2
        assert "platform_name.lower()" in content
        assert "is_relay" in content
        assert "delivered = True" in content
        # A failing card send must fall through to the native lane, never raise.
        assert "_lark_cron_sent = False" in content
        assert "if _lark_cron_sent:" in content

    def test_cron_split_lanes_stay_fail_open(self, scheduler_copy: Path) -> None:
        """Both injected lanes swallow plugin errors so native delivery still runs."""
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        content = self._cron_target(scheduler_copy).read_text(encoding="utf-8")
        search_from = 0
        lanes = 0
        while (start := content.find(MK_CRON_DELIVER, search_from)) != -1:
            end = content.index(MK_CRON_DELIVER_END, start)
            block = content[start:end]
            lanes += 1
            assert "except Exception:" in block
            assert "_lark_cron_sent = False" in block
            search_from = end + len(MK_CRON_DELIVER_END)
        assert lanes == 2


class TestCronBackupRestore:
    def test_backup_created_on_apply(self, scheduler_copy: Path) -> None:
        cp = _cron_patcher(scheduler_copy)
        cp.apply()
        target = cp.cron_path
        assert target.name == "scheduler_delivery.py"
        assert target.with_suffix(target.suffix + ".hermes_lark.bak").exists()

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
