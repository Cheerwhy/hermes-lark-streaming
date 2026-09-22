"""Revision-pinned Hermes fixtures: reuse verified local files, download missing ones."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from functools import cache
from pathlib import Path

from hermes_lark_streaming.split_gateway import GATEWAY_FILES, inject_gateway

SAMPLES_DIR = Path(__file__).parent / "samples"
MANIFEST_PATH = Path(__file__).with_suffix(".json")
TREE_FILES = (
    "gateway/run.py",
    *(f"gateway/{name}" for name in GATEWAY_FILES),
    "cron/scheduler.py",
    "cron/scheduler_delivery.py",
)


@cache
def source_at(relative: str) -> str:
    """Validate cached sources; download missing files from the pinned commit only."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = manifest["sha256"][relative]
    path = SAMPLES_DIR / relative
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        revision = manifest["revision"]
        url = f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{revision}/{relative}"
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = response.read()
        except (OSError, urllib.error.URLError) as exc:
            raise RuntimeError(f"Cannot download Hermes fixture {revision}:{relative}: {exc}") from exc
        _validate(relative, data, expected)
        # Publish only a complete, verified download; interruption cannot poison the cache.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
    else:
        _validate(relative, data, expected)
    return data.decode("utf-8")


def _validate(relative: str, data: bytes, expected: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(
            f"Hermes fixture checksum mismatch: {relative}; "
            "remove the cached file to download the pinned version again"
        )


@cache
def patched_gateway(filename: str) -> str:
    """Cache generated text only; execution namespaces remain isolated per test."""
    return inject_gateway(filename, source_at(f"gateway/{filename}"))
