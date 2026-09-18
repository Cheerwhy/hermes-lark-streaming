"""Revision-pinned upstream fixtures, independent of installed hooks and backups."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from functools import cache
from pathlib import Path

import pytest

LEGACY_REVISION = "b20cc5f787ea816ea8645603b7b2ac8234dcb8b4"
SPLIT_REVISION = "2237be355906fbe6065ce1815711eee52b2d646e"  # Hermes v0.21.1
SPLIT_LEDGER_REVISION = "13c580422c0b28a78d42b0e10decad6f121e6c45"  # Queued inbound delivery ledger
TARGET_REVISION = "345cd2b057a452236de401d3534b8502a7465e8d"  # Generation-scoped one-turn overrides


@cache
def source_at(relative: str, revision: str) -> str:
    root = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "hermes-agent"
    if root.is_dir():
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{revision}:{relative}"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    url = f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{revision}/{relative}"
    try:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    content = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if not content:
            raise ValueError("Empty upstream source")
        return content
    except Exception as exc:
        message = f"Cannot load pinned Hermes fixture {revision}:{relative}: {exc}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
