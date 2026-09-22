"""Shared isolated filesystem fixtures; pinned sources are cached in memory."""

from pathlib import Path

import pytest
from hermes_sources import TREE_FILES, source_at


def pytest_addoption(parser):
    parser.addoption("--local-hermes", action="store_true", help="Also verify an isolated copy of local Hermes")


@pytest.fixture
def hermes_tree(tmp_path: Path) -> Path:
    for relative in TREE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source_at(relative), encoding="utf-8")
    return tmp_path
