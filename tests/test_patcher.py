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

import pytest

from hermes_lark_streaming.patcher import MARKERS, Patcher, PatcherError

RUN_SRC = Path.home() / ".hermes" / "hermes-agent" / "gateway" / "run.py"
RUN_BAK = RUN_SRC.with_suffix(RUN_SRC.suffix + ".hermes_lark.bak")
SAMPLES_DIR = Path(__file__).parent / "samples"
SAMPLE_RUN = SAMPLES_DIR / "run.py"

_RUN_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/gateway/run.py"


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


def _patcher(path: Path) -> Patcher:
    return Patcher(run_path=path)


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

    def test_apply_idempotent(self, run_copy: Path) -> None:
        patcher = _patcher(run_copy)
        patcher.apply()
        after_first = run_copy.read_text(encoding="utf-8")
        patcher.apply()  # second apply should be no-op
        after_second = run_copy.read_text(encoding="utf-8")
        assert after_first == after_second

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
