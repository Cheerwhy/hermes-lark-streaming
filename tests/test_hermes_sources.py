"""Keep fixture downloads reproducible without hiding unavailable revisions."""

import io
import urllib.error
from unittest.mock import Mock

import hermes_sources
import pytest


@pytest.mark.parametrize("status", [429, 503, 404])
def test_pinned_download_retries_only_transient_errors(monkeypatch, tmp_path, status):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CI", "true")
    error = urllib.error.HTTPError("https://example.test/source", status, "failed", None, None)
    fetch = Mock(side_effect=[error, io.BytesIO(b"# pinned source\n")])
    delay = Mock()
    monkeypatch.setattr(hermes_sources.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(hermes_sources.time, "sleep", delay)
    revision = f"download-test-{status}"
    if status == 404:
        with pytest.raises(pytest.fail.Exception, match="Cannot load pinned Hermes fixture"):
            hermes_sources.source_at("gateway/run.py", revision)
        assert fetch.call_count == 1
        delay.assert_not_called()
    else:
        assert hermes_sources.source_at("gateway/run.py", revision) == "# pinned source\n"
        assert fetch.call_count == 2
        delay.assert_called_once_with(1)
        assert hermes_sources.source_at("gateway/run.py", revision) == "# pinned source\n"
        assert fetch.call_count == 2
