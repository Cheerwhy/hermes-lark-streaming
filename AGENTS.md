# AGENTS.md

## Project

Hermes Gateway plugin that injects hooks into `~/.hermes/hermes-agent/gateway/run.py` via AST patching to provide real-time streaming Feishu/Lark CardKit v2.0 cards with typewriter effect.

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
$HERMES_PYTHON -m pip install -e ".[dev]"  # test dependencies

# Lint
$HERMES_PYTHON -m ruff check hermes_lark_streaming/
$HERMES_PYTHON -m mypy hermes_lark_streaming/

# Run tests (local run.py first, CI auto-downloads from GitHub)
$HERMES_PYTHON -m pytest tests/ -v
```

## Architecture

```
gateway/run.py (Hermes)
  └─ AST-injected hooks (patcher.py defines markers + injection logic)
       │
       ├─ on_feishu_normalize   → patch.on_feishu_normalize() (inline, fixes false thread_id)
       ├─ on_message_started    → controller.on_message_started()
       ├─ on_tool_updated       → controller.on_tool_update()
       ├─ on_answer_delta       → controller.on_answer()
       ├─ on_thinking_delta     → controller.on_thinking()
       ├─ on_reasoning_delta    → controller.on_reasoning()
       ├─ on_background_review_message → controller.defer_background_review()
       ├─ on_message_interrupted → controller.on_interrupted()
       ├─ on_message_completed  → controller.on_completed()
       └─ on_message_aborted    → controller.on_aborted()

StreamCardController (singleton, controller.py)
  ├─ CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  │   └─ linear mode: CardSession.linear + CardSession.linear_state (LinearState)
  ├─ _interrupt_map — old_message_id → new_message_id mapping for interrupt redirect
  ├─ FlushController (flush.py) — throttles card updates (100ms CardKit / 1.5s IM fallback)
  ├─ TextState (text.py) — accumulates streaming text, tracks dirty state
  ├─ ToolUseTracker (tooluse.py) — tracks tool call lifecycle with icon/status mapping
  ├─ UnavailableGuard — auto-terminates on message delete/recall
  └─ ImageResolver (image.py) — async download + re-upload markdown images as Feishu img_key

Linear mode (controller_linear_mixin.py + linear.py)
  ├─ LinearState — flat segment list (reasoning / answer / tool), same-type appends, cross-type creates new
  ├─ _do_linear_flush — 3-step pipeline: batch add elements → stream text → batch update tool panels
  └─ _do_linear_complete — close streaming + full card rebuild (retry + streaming_closed idempotency)

FeishuClient (feishu.py) — lark-oapi SDK wrapper
  ├─ CardKit streaming API (preferred) — update single elements at 100ms intervals
  └─ IM PATCH fallback — rebuild entire card at 1.5s intervals

Card templates (cardkit.py) — builds Feishu card JSON
  ├─ build_streaming_card / build_streaming_card_v2 — during generation
  ├─ build_complete_card — final card with reasoning panel, tool panel, footer
  └─ build_linear_complete_card — linear mode final card, renders segments in order
```

## Key Constraints

- Hermes `>= 0.11.0` (2026.4.23) required. `patcher.py` targets specific function names in Hermes's `gateway/run.py` (`_handle_message_with_agent`, `progress_callback`, `_stream_delta_cb`, `_interim_assistant_cb`). If Hermes changes these, `verify` will catch it.
- The interrupt hook is injected at the `"Restart typing indicator"` comment in `_run_agent`. It fires when `was_interrupted and next_message_id` are both truthy. The `_interrupt_map` redirects `on_completed(old_id)` to the new session, handling nested interrupts (A→B→C).
- The `_thinking_hook` has a `not already_streamed` guard (patcher.py:103) — thinking deltas are skipped once answer streaming has begun.
- The NORMALIZE hook (`on_feishu_normalize`) is injected at `source = event.source` in `_handle_message`, before any other processing. It detects Feishu quoted messages with a false `thread_id` (set by the Feishu adapter but absent in raw event) and clears it, preventing `_reply_anchor_for_event` from returning the wrong ID.
- The `anchor_id` mechanism: for Feishu quoted messages, `_reply_anchor_for_event(event)` returns `reply_to_message_id` instead of `event.message_id`. The START hook passes both — `message_id` for session identity and streaming callback lookup, `anchor_id` for card delivery (reply target). Sessions are registered under both keys.
- Reasoning display depends on upstream providing `<thinking>`/`<thought>`/`<antthinking>` tags or `Reasoning:\n` prefix in text. Native API reasoning blocks (Anthropic extended thinking, DeepSeek reasoning_content) are available via `on_reasoning_delta` hook when `display.platforms.feishu.show_reasoning` is enabled.
- CardKit v2.0 elements (collapsible_panel, streaming_mode) only work with `"schema": "2.0"` cards. IM fallback path uses v1 card format.
- Linear mode (default) uses a single card for the entire message lifecycle: elements are dynamically created in event arrival order. Non-linear mode creates a streaming card then replaces it with a completion card. When linear CardKit creation fails, it falls back to non-linear mode.
- Commit messages: body should use bullet list format (unnumbered `- item`).
