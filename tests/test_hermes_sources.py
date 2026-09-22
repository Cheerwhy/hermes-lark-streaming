"""Pinned cache reuse and download failures, with all network calls isolated."""

import hashlib
import io
import json
import urllib.error
from unittest.mock import Mock

import hermes_sources
import pytest


def test_all_pinned_files_match_manifest():
    manifest = json.loads(hermes_sources.MANIFEST_PATH.read_text())
    assert manifest["version"] == "0.21.1"
    assert len(manifest["revision"]) == 40
    assert set(hermes_sources.TREE_FILES) <= manifest["sha256"].keys()
    for relative in manifest["sha256"]:
        assert hermes_sources.source_at(relative)


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    data = b"# pinned source\n"
    relative = "gateway/run.py"
    manifest = {
        "revision": "2237be355906fbe6065ce1815711eee52b2d646e",
        "sha256": {relative: hashlib.sha256(data).hexdigest()},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    root = tmp_path / "samples"
    monkeypatch.setattr(hermes_sources, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(hermes_sources, "SAMPLES_DIR", root)
    fetch = Mock(return_value=io.BytesIO(data))
    monkeypatch.setattr(hermes_sources.urllib.request, "urlopen", fetch)
    hermes_sources.source_at.cache_clear()
    try:
        yield root / relative, relative, data, fetch
    finally:
        hermes_sources.source_at.cache_clear()


def test_cached_source_never_downloads(cache_env):
    path, relative, data, fetch = cache_env
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    assert hermes_sources.source_at(relative) == data.decode()
    fetch.assert_not_called()


def test_missing_source_downloads_pinned_revision_and_reuses_disk_cache(cache_env):
    path, relative, data, fetch = cache_env
    assert hermes_sources.source_at(relative) == data.decode()
    assert path.read_bytes() == data
    fetch.assert_called_once_with(
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/"
        "2237be355906fbe6065ce1815711eee52b2d646e/gateway/run.py", timeout=10,
    )
    hermes_sources.source_at.cache_clear()
    assert hermes_sources.source_at(relative) == data.decode()
    assert fetch.call_count == 1
    assert list(path.parent.iterdir()) == [path]


def test_modified_cache_is_not_silently_replaced(cache_env):
    path, relative, _, fetch = cache_env
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# another revision\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        hermes_sources.source_at(relative)
    assert path.read_bytes() == b"# another revision\n"
    fetch.assert_not_called()


def test_invalid_download_does_not_poison_cache(cache_env):
    path, relative, data, fetch = cache_env
    fetch.side_effect = [io.BytesIO(b"invalid"), io.BytesIO(data)]
    with pytest.raises(ValueError, match="checksum mismatch"):
        hermes_sources.source_at(relative)
    assert not path.exists()
    assert hermes_sources.source_at(relative) == data.decode()
    assert path.read_bytes() == data


@pytest.mark.parametrize("error", [TimeoutError("timeout"), urllib.error.URLError("unavailable")])
def test_download_failure_is_explicit_without_partial_cache(cache_env, error):
    path, relative, _, fetch = cache_env
    fetch.side_effect = error
    with pytest.raises(RuntimeError, match="Cannot download Hermes fixture"):
        hermes_sources.source_at(relative)
    assert not path.exists()
    assert fetch.call_count == 1
