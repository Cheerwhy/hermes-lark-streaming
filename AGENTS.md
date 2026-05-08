# AGENTS.md

## Project

Hermes Gateway plugin that injects 6 hooks into `~/.hermes/hermes-agent/gateway/run.py` via AST patching to provide real-time streaming Feishu/Lark CardKit v2.0 cards with typewriter effect.

## Commands

```bash
# All commands must use Hermes's venv Python
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

$HERMES_PYTHON -m hermes_lark_streaming verify     # Check compatibility (safe, no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks into run.py
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore run.py from .hermes_lark.bak
$HERMES_PYTHON -m hermes_lark_streaming status     # Show patch status

# Install for development
$HERMES_PYTHON -m pip install -e .

# Run tests (copies real run.py from Hermes env to tests/samples/ each time)
$HERMES_PYTHON -m pytest tests/test_patcher.py -v
```

## Architecture

```
gateway/run.py (Hermes)
  └─ AST-injected hooks (patcher.py defines markers + injection logic)
       │
       ├─ on_message_started    → controller.on_message_started()
       ├─ on_tool_updated       → controller.on_tool_update()
       ├─ on_answer_delta       → controller.on_answer()
       ├─ on_thinking_delta     → controller.on_thinking()
       ├─ on_message_completed  → controller.on_completed()
       └─ on_message_aborted    → controller.on_aborted()

StreamCardController (singleton, controller.py)
  ├─ CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  ├─ FlushController (flush.py) — throttles card updates (100ms CardKit / 1.5s IM fallback)
  ├─ TextState (text.py) — accumulates streaming text, tracks dirty state
  ├─ ToolUseTracker (tooluse.py) — tracks tool call lifecycle with icon/status mapping
  ├─ UnavailableGuard — auto-terminates on message delete/recall
  └─ ImageResolver (image.py) — async download + re-upload markdown images as Feishu img_key

FeishuClient (feishu.py) — lark-oapi SDK wrapper
  ├─ CardKit streaming API (preferred) — update single elements at 100ms intervals
  └─ IM PATCH fallback — rebuild entire card at 1.5s intervals

Card templates (cardkit.py) — builds Feishu card JSON
  ├─ build_streaming_card / build_streaming_card_v2 — during generation
  └─ build_complete_card — final card with reasoning panel, tool panel, footer
```

## Key Constraints

- `patcher.py` targets specific function names in Hermes's `gateway/run.py` (`_handle_message_with_agent`, `progress_callback`, `_stream_delta_cb`, `_interim_assistant_cb`). If Hermes changes these, `verify` will catch it.
- The `_thinking_hook` has a `not already_streamed` guard (patcher.py:103) — thinking deltas are skipped once answer streaming has begun.
- Reasoning display depends on upstream providing `<thinking>`/`<thought>`/`<antthinking>` tags or `Reasoning:\n` prefix in text. The Hermes gateway does NOT set `reasoning_callback` on the agent, so native API thinking blocks are not available during streaming.
- CardKit v2.0 elements (collapsible_panel, streaming_mode) only work with `"schema": "2.0"` cards. IM fallback path uses v1 card format.
- Commit messages: body should use bullet list format (unnumbered `- item`).
