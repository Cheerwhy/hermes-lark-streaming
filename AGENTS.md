# AGENTS.md

## Project

Hermes Gateway plugin that injects hooks into `~/.hermes/hermes-agent/gateway/run.py` and `cron/scheduler.py` via AST patching to provide real-time streaming Feishu/Lark CardKit v2.0 cards with typewriter effect.

## Commands

```bash
# All commands must use Hermes's venv Python
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

$HERMES_PYTHON -m hermes_lark_streaming verify     # Check compatibility (safe, no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks into run.py and cron/scheduler.py
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore from .hermes_lark.bak backup
$HERMES_PYTHON -m hermes_lark_streaming status     # Show patch status

# Install for development
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m pip install -e ".[dev]"  # test dependencies

# Lint
$HERMES_PYTHON -m ruff check hermes_lark_streaming tests
$HERMES_PYTHON -m mypy hermes_lark_streaming/

# Run tests (local run.py first, CI auto-downloads from GitHub)
$HERMES_PYTHON -m pytest tests/ -q
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
       ├─ on_queued_followup_boundary → patch.on_queued_followup_boundary() (finalize card before drain, set response_previewed/already_sent)
       ├─ on_queued_followup_result   → patch.on_queued_followup_result() (carry deepest completion ID through recursive merge)
       ├─ on_message_completed_wait → controller.on_completed_wait()
       ├─ on_message_aborted    → controller.on_aborted()
       └─ on_background_deliver → controller.on_background_deliver()

cron/scheduler.py (Hermes)
  └─ CronPatcher (patcher.py) injects on_cron_deliver into _deliver_result
       └─ intercepts feishu/lark targets → build_cron_card → send_card_to_chat

StreamCardController (singleton, controller.py)
  ├─ CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  │   └─ stream segments: CardSession.segment_state (SegmentState)
  ├─ _interrupt_map — old_message_id → new_message_id mapping for interrupt redirect
  ├─ FlushController (streaming/flush.py) — throttles CardKit updates (100ms)
  ├─ ToolUseTracker (streaming/tooluse.py) — tracks tool call lifecycle with icon/status mapping
  ├─ UnavailableGuard (streaming/unavailable_guard.py) — auto-terminates on message delete/recall
  └─ ImageResolver (streaming/image.py) — async download + re-upload markdown images as Feishu img_key

Streaming card runtime (streaming/)
  ├─ controller.py — StreamingController: create card, flush, split/rollover, and cron delivery orchestration
  ├─ session.py — CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  ├─ segments.py — SegmentState: flat segment list (reasoning / answer / tool), same-type appends, cross-type creates new
  ├─ segment_helper.py — CardKit action builders, element estimates, and tool split point selection
  ├─ text.py — reasoning tag parsing and final answer text cleanup
  ├─ flush.py — FlushController: throttles CardKit updates (100ms)
  ├─ tooluse.py — ToolUseTracker: tool call lifecycle tracking with icon/status mapping
  ├─ image.py — ImageResolver: async download + re-upload markdown images as Feishu img_key
  └─ unavailable_guard.py — UnavailableGuard: auto-terminates on message delete/recall

FeishuClient (feishu.py) — lark-oapi SDK wrapper
  ├─ CardKit streaming API — update single elements at 100ms intervals

Card templates (cardkit/)
  ├─ builder.py — builds Feishu card JSON
  │   ├─ _build_header — card-level header with status-based theming (blue/green/red)
  │   ├─ build_streaming_card_v2 — initial streaming CardKit v2 card (header_enabled, text_size)
  │   ├─ build_complete_card — final card, renders segments in order (header_enabled, body_text_size, footer_enabled, footer_text_size)
  │   ├─ build_cron_card — static card for cron delivery
  │   └─ build_background_card — static card for background task delivery
  ├─ markdown.py — CardKit markdown normalization and table/image helpers
  └─ i18n.py — localized CardKit labels
```

## Key Constraints

- Hermes `>= 0.14.0` (2026.5.16) required. `patcher.py` targets specific function names in Hermes's `gateway/run.py` (`_handle_message_with_agent`, `progress_callback`, `_stream_delta_cb`, `_interim_assistant_cb`) and `cron/scheduler.py` (`_deliver_result`). If Hermes changes these, `verify` will catch it.
- The interrupt hook is injected at the `"Restart typing indicator"` comment in `_run_agent`. It fires when `was_interrupted and next_message_id` are both truthy. The `_interrupt_map` redirects completion from `old_id` to the new session, handling nested interrupts (A→B→C).
- The completion hook installed into `gateway/run.py` is async: `on_message_completed_wait` awaits queued CardKit creation/finalization before setting `already_sent`. Upgrades must rerun `uninstall` + `install` so older sync completion hooks are removed from Hermes gateway.
- The `_thinking_hook` has a `not already_streamed` guard (patcher.py:103) — thinking deltas are skipped once answer streaming has begun.
- The NORMALIZE hook (`on_feishu_normalize`) is injected at `source = event.source` in `_handle_message`, before any other processing. It detects Feishu quoted messages with a false `thread_id` (set by the Feishu adapter but absent in raw event) and clears it, preventing `_reply_anchor_for_event` from returning the wrong ID.
- The `anchor_id` mechanism: for Feishu quoted messages, `_reply_anchor_for_event(event)` returns `reply_to_message_id` instead of `event.message_id`. The START hook passes both — `message_id` for session identity and streaming callback lookup, `anchor_id` for card delivery (reply target). Sessions are registered under both keys.
- Reasoning display depends on upstream providing `<thinking>`/`<thought>`/`<antthinking>` tags or `Reasoning:\n` prefix in text. Native API reasoning blocks (Anthropic extended thinking, DeepSeek reasoning_content) are available via `on_reasoning_delta` hook when `display.platforms.feishu.show_reasoning` is enabled.
- CardKit v2.0 elements (collapsible_panel, streaming_mode) only work with `"schema": "2.0"` cards.
- Streaming cards use a single CardKit card for the message lifecycle: elements are dynamically created in event arrival order. When CardKit creation fails, the plugin yields to the Hermes Gateway default reply.
- The follow-up drain hooks manage card lifecycle for Hermes's queued follow-up messages (triggered when `busy_text_mode: queue` or `busy_input_mode: queue`). `on_queued_followup_boundary` is injected at `was_interrupted = result.get("interrupted")` in `_run_agent` — it finalizes the current card and sets `response_previewed`/`already_sent` on the result dict before the drain loop processes the queued message. `on_queued_followup_result` is injected at `return _preserve_queued_followup_history_offset(...)` and uses `setdefault` to carry the deepest `_hermes_lark_completion_id` back through the recursive merge chain.
- The COMPLETE hook uses `_lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id` — in follow-up scenarios the deepest message_id propagates up via `on_queued_followup_result`, ensuring the correct card session is finalized. Non-follow-up scenarios fall back to `event.message_id`.
- The background deliver hook (`on_background_deliver`) is injected in `_run_background_task` after `adapter.extract_images(response)`. It uses `ReplyMessage` API with `event_message_id` as anchor, so cards land in the correct topic. On success, `text_content` is cleared to avoid duplicate text delivery, while images and media files continue through the original Hermes loops. On failure, the original Hermes delivery logic runs as fallback.
- Commit messages: body should use bullet list format (unnumbered `- item`).
