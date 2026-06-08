"""Patcher tests — copy real run.py, apply/remove/verify against the copy.

Usage:
    ~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_patcher.py -v
"""

from __future__ import annotations

import ast
import shutil
import textwrap
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_lark_streaming.patcher import (
    MARKERS,
    MK_CRON_DELIVER,
    MK_CRON_DELIVER_END,
    CronPatcher,
    Patcher,
    PatcherError,
    _cron_deliver_hook,
    _remove_block,
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
    namespace: dict = {}
    source = (
        "def deliver(targets, cleaned_delivery_content, loop):\n"
        "    fallback = []\n"
        "    for platform_name, chat_id in targets:\n"
        "        delivered = False\n"
        f"{_cron_deliver_hook('        ')}"
        "        fallback.append(chat_id)\n"
        "    return fallback\n"
    )
    exec(compile(source, "<cron-hook-test>", "exec"), namespace)
    return namespace["deliver"]


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
        """)
        )
        with pytest.raises(PatcherError, match="reasoning_config"):
            _patcher(p).verify_target()


class TestApplyRemove:
    def test_apply_injects_all_markers(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        content = run_copy.read_text(encoding="utf-8")
        for begin, end in MARKERS:
            assert begin in content, f"Missing marker: {begin}"
            assert end in content, f"Missing marker: {end}"

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
        assert "_lark_next_message_id = getattr(pending_event, 'message_id', None) or next_message_id" in content
        assert "new_message_id=_lark_next_message_id" in content
        assert "anchor_id=_lark_next_anchor_id" in content
        assert "on_message_completed_wait(" in content
        assert "on_message_needs_text_fallback" in content
        assert "_lark_card_sent = await on_message_completed_wait(" in content
        assert "agent_result.pop('already_sent', None)" in content
        assert "message_id=event.message_id" in content
        assert "on_answer_delta(message_id=event_message_id" in content
        assert "on_thinking_delta(message_id=event_message_id" in content
        assert "on_reasoning_delta(message_id=event_message_id" in content
        assert "on_background_deliver(" in content
        assert "_bg_preview = prompt[:60] + ('...' if len(prompt) > 60 else '')" in content
        assert "content=text_content" in content
        assert "reply_to_message_id=event_message_id" in content
        assert "if not images and not media_files:" in content

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
        assert "delivered = True" in content

    def test_injected_hook_skips_duplicate_card_target(self) -> None:
        deliver = _build_cron_hook_runner()
        sent = []

        def fake_on_cron_deliver(*, chat_id, content, loop):
            sent.append((chat_id, content))
            return True

        targets = [("feishu", "oc_same"), ("feishu", "oc_same")]
        with patch(
            "hermes_lark_streaming.patch.on_cron_deliver",
            side_effect=fake_on_cron_deliver,
        ):
            fallback = deliver(targets, " failed ", object())

        assert sent == [("oc_same", "failed")]
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

    def test_returns_false_when_no_loop(self) -> None:
        from hermes_lark_streaming.patch import on_cron_deliver

        with patch("hermes_lark_streaming.patch.get_controller") as mock_get:
            ctrl = MagicMock()
            ctrl.enabled = True
            mock_get.return_value = ctrl
            assert on_cron_deliver(chat_id="c1", content="text", loop=None) is False

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
            ctrl.on_cron_deliver.assert_called_once_with(chat_id="c1", content="hello", loop=loop)
