# Hermes 0.21.1 source fixtures

Unmodified source files from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/2237be355906fbe6065ce1815711eee52b2d646e).
Revision: `2237be355906fbe6065ce1815711eee52b2d646e` (CLI version 0.21.1, release date 2026.9.7).
`tests/hermes_sources.json` records the revision and SHA-256 of every source file
and the upstream MIT license. Downloaded files, including `LICENSE`, are cached
under `tests/samples/`, which is excluded from Git.

The gateway/cron targets exercise patch installation, removal, restore and
rollback. The stream-consumer, response-filter and agent helpers exercise actual
upstream text delivery with external I/O mocked. No upstream package is installed.

Tests prefer existing files after checksum validation. Missing files are downloaded
from the pinned commit with a 10-second request timeout, verified, and saved atomically.
A complete cache runs offline. Download errors fail explicitly; mismatched cached
files fail without silently replacing them (delete that file to download it again).
Tests never read local Hermes backups. Generated code and compiled methods may be
cached, but execution namespaces and controller state are per-test.

CI caches `tests/samples/` using the manifest hash, so unchanged fixtures are reused.

To update deliberately, download only the listed paths and LICENSE from one
reviewed commit using its immutable raw GitHub URLs. Verify the upstream CLI
version, regenerate the manifest hashes, and review the baseline behavior and tests
together. Do not mix files from different revisions or update this baseline just
because the local Hermes installation changed. The baseline remains the minimum
supported version; newer upstream seams can have small synthetic regression tests.

Optional local-installation smoke test (copies sources to a temporary directory):

```bash
~/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/test_multifile_patcher.py -k installed_hermes --local-hermes -q
```

The daily `hermes-check.yml` workflow independently checks current upstream anchors.
