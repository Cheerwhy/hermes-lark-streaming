# AGENTS.md

## Project

Hermes >= 0.22.0 native Feishu/Lark streaming-card platform plugin. Register a subclass of the built-in FeishuAdapter under the existing `feishu` identity. Keep the renderer compact; no AST, core-source writes, monkey-patching or sidecar.

## Development

Use an isolated Python environment derived from Hermes's managed Python for local tests. Do not pip-install development dependencies into the user's live managed environment. Install the fixed Hermes 0.22.0 source and this package in the test environment.

```bash
python -m ruff check hermes_lark_streaming tests scripts __init__.py
python -m mypy hermes_lark_streaming scripts
python -m pytest tests -q
python -m hermes_lark_streaming verify
```

## Architecture and constraints

- `plugin.py`: official registration, host capability/version checks
- `adapter.py`: inherit built-in intake/auth/media and override native stream/static presentation
- `controller.py`: per-adapter, per-turn state and idempotent completion
- `hooks.py`: official contextvars correlate observations by inbound message; agent and consumer turn IDs differ
- `sender.py`: standalone cron delivery with media preserved
- `streaming/`, `cardkit/`, `feishu.py`: existing renderer and CardKit APIs
- Keep `feishu` identity to preserve authorization, session bindings, tool presets and Cron targets
- Plugin options live under `platforms.feishu.extra.card`; do not rewrite user config at load time
- Text frames are cumulative; tool boundaries are not terminal; approval boundaries may explicitly reseed a stream
- Do not correlate concurrent runs only by chat ID; do not show fabricated usage
- Legacy injections must be uninstalled with the old release before upgrading
- Unit tests mock network APIs; integration tests run against real host interfaces. Report live validation separately
- 默认中文回复；提交用 Conventional Commits，正文使用简洁的 `-` 无序列表
