# AGENTS.md

## Project

Hermes Gateway plugin that injects hooks into Hermes split gateway modules and `cron/scheduler_delivery.py` via AST patching to provide real-time streaming Feishu/Lark CardKit v2.0 cards with typewriter effect.

## Commands

```bash
# All commands must use Hermes's venv Python
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3

$HERMES_PYTHON -m hermes_lark_streaming verify     # Check compatibility (safe, no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks into split gateway and cron delivery modules
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore from .hermes_lark.bak backup
$HERMES_PYTHON -m hermes_lark_streaming status     # Show patch status

# Install for development
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m pip install -e ".[dev]"  # test dependencies

# Lint
$HERMES_PYTHON -m ruff check hermes_lark_streaming tests
$HERMES_PYTHON -m mypy hermes_lark_streaming/

# Run tests (reuse tests/samples cache; download missing files at pinned 0.21.1 commit)
$HERMES_PYTHON -m pytest tests/ -q

# Optional local Hermes smoke test; only an isolated temporary copy is patched
$HERMES_PYTHON -m pytest tests/test_multifile_patcher.py -k installed_hermes --local-hermes -q
```

## Architecture

```
gateway/run_inbound.py, run_turn.py, run_turn_runner.py, run_busy.py (Hermes)
  └─ AST-injected hooks (patcher.py defines markers; split_gateway.py locates anchors)
       │
       ├─ on_feishu_normalize   → patch.on_feishu_normalize() (inline, fixes false thread_id)
       ├─ on_message_started    → controller.on_message_started()
       ├─ on_tool_updated       → controller.on_tool_update()
       ├─ on_answer_delta       → controller.on_answer()
       ├─ on_thinking_delta     → controller.on_thinking()
       ├─ on_reasoning_delta    → controller.on_reasoning()
       ├─ on_background_review_message → controller.defer_background_review()
       ├─ on_message_interrupted → controller.on_interrupted()
       ├─ on_queued_followup_result   → patch.on_queued_followup_result() (carry deepest completion ID through recursive merge)
       ├─ on_message_completed_wait → controller.on_completed_wait()
       ├─ on_message_aborted    → controller.on_aborted()
       ├─ on_session_aborted    → controller.on_session_aborted() (busy-session /stop)
       └─ on_background_deliver → controller.on_background_deliver()

cron/scheduler_delivery.py (Hermes)
  └─ CronPatcher + split_cron inject into live and standalone delivery lanes
       └─ intercepts feishu/lark targets → build_cron_card → send_card_to_chat

StreamCardController (singleton, controller.py)
  ├─ CardSession per message (state machine: IDLE→CREATING→STREAMING→COMPLETED/FAILED/ABORTED)
  │   └─ stream segments: CardSession.segment_state (SegmentState)
  ├─ _session_keys — Hermes session_key → active CardSession mapping for precise /stop handling
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
  │   ├─ build_streaming_card_v2 — initial loading CardKit v2 card (header_enabled, width_mode)
  │   ├─ build_complete_card — final card, renders segments in order (header_enabled, body_text_size, footer_enabled, footer_text_size)
  │   ├─ build_cron_card — static card for cron delivery
  │   └─ build_background_card — static card for background task delivery
  ├─ markdown.py — CardKit markdown normalization and table/image helpers
  └─ i18n.py — localized CardKit labels
```

## Key Constraints

- Hermes `>= 0.21.1` (2026.9.7) split layout is required. `split_gateway.py` and `split_cron.py` validate function-scoped AST anchors and reject missing or ambiguous matches. `gateway/run.py` and `cron/scheduler.py` are entry points, not injection targets.
- The interrupt hook runs before the recursive `_run_agent` call in `_run_agent_queued_followup`. It separates inbound identity from the reply anchor and starts or redirects the next card. `_interrupt_map` handles nested interrupts (A→B→C).
- The completion hook in `run_turn.py` is async: `on_message_completed_wait` awaits card creation/finalization before setting `already_sent`. Reinstall hooks after upgrading. Legacy markers remain removable; `on_queued_followup_boundary` remains only as a shim for previously installed hooks.
- The split interim callback uses `not already_streamed` to avoid duplicating answer text as thinking, while preserving streaming TTS boundaries.
- NORMALIZE runs at both `source = event.source` admission sites in `_hm_admit_event` (`run_inbound.py`). It clears false Feishu quote thread IDs before routing so reply anchors remain correct.
- The `anchor_id` mechanism: for Feishu quoted messages, `_reply_anchor_for_event(event)` returns `reply_to_message_id` instead of `event.message_id`. The START hook passes both — `message_id` for session identity and streaming callback lookup, `anchor_id` for card delivery (reply target). Sessions are registered under both keys.
- Reasoning display depends on upstream providing `<thinking>`/`<thought>`/`<antthinking>` tags or `Reasoning:\n` prefix in text. Native API reasoning blocks (Anthropic extended thinking, DeepSeek reasoning_content) are available via `on_reasoning_delta` hook when `display.platforms.feishu.show_reasoning` is enabled.
- CardKit v2.0 elements (collapsible_panel, streaming_mode) only work with `"schema": "2.0"` cards.
- Streaming cards use a single CardKit card for the message lifecycle: elements are dynamically created in event arrival order. When CardKit creation fails, the plugin yields to the Hermes Gateway default reply.
- Follow-up completion runs in `_run_agent_deliver_first_response` before native delivery, setting `response_previewed`/`already_sent` without destroying attachment-bearing text. `on_queued_followup_result` runs after recursive completion and uses `setdefault` to preserve the deepest completion identity.
- The COMPLETE hook uses `_lark_completion_id = agent_result.get('_hermes_lark_completion_id') or event.message_id` — in follow-up scenarios the deepest message_id propagates up via `on_queued_followup_result`, ensuring the correct card session is finalized. Non-follow-up scenarios fall back to `event.message_id`.
- Background delivery runs after `adapter.extract_images(response)` in `_run_background_task_inner`. On successful card delivery only text is cleared; native image/media delivery continues. Failed card delivery falls back to Hermes.
- Commit messages: body should use bullet list format (unnumbered `- item`).
