# Hermes Lark Streaming

[![PyPI](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time streaming card plugin for [Hermes](https://github.com/NousResearch/hermes-agent) Gateway via Feishu/Lark CardKit v2.0.

Inspired by [openclaw-lark](https://github.com/larksuite/openclaw-lark) and [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card).

[中文文档](README.md)

![](assets/cover.jpg)

---

## Features

- **Streaming output** — AI responses rendered in real-time interactive cards with typewriter effect
- **Streaming cards** — Dynamically renders thinking, tool calls, and answer elements in event arrival order within a single card
- **Reasoning display** — Shows model thinking/reasoning content
- **Tool use tracking** — Live tool call status with standard icons, result/error blocks
- **CardKit v2.0** — Uses Feishu CardKit streaming API; card creation failures yield to the Hermes Gateway default reply
- **Completion card** — Final card with token usage, duration, and context info
- **Card style** — Configurable card header/footer toggle and body/footer text sizes
- **Message guard** — Auto-terminates updates when message is deleted/recalled
- **Image resolution** — Detects markdown image references, downloads and re-uploads as Feishu img_key
- **Abort handling** — Gracefully handles `/stop` command and message interrupts with aborted state card and automatic new session
- **Cron card delivery** — Delivers scheduled job results as Feishu cards, preserving Markdown rendering
- **Background task card delivery** — Delivers `/background` (`/btw`) task results as cards, with topic-aware reply
- **i18n** — Built-in Chinese/English bilingual card text (status, tool panel, thinking labels, etc.) that auto-switches based on Feishu client language

---

## Card Rendering

The plugin dynamically renders thinking, tool call, and answer elements in event arrival order, keeping multi-round content in its actual order.

When long conversations or excessive tool steps cause the card to approach Feishu's 200-element limit, it automatically splits into multiple cards: the old card is sealed with complete data, a new card continues output, and only the last card includes the footer. Oversized tool panels are also split at step boundaries.

![](assets/streaming.jpg)

---

## Requirements

- Hermes `>= 0.11.0` (2026.4.23) with Feishu/Lark platform configured
- `Python >= 3.11`
- `lark-oapi >= 1.4.0` — Feishu/Lark official Python SDK
- `PyYAML >= 6.0` — YAML parser
- Feishu app permissions: CardKit read/write, message send & reply, image upload

---

## Installation

> **Note:** Hermes runs in its own Python venv. Install the plugin using Hermes's Python, or the gateway will fail to load it at runtime.

### AI Agent

Tell your agent to read the README and follow the manual steps:

```
curl https://raw.githubusercontent.com/Cheerwhy/hermes-lark-streaming/main/README.md
```

### Manual

> The plugin reads `HERMES_HOME` from Hermes to locate the installation path (default: `~/.hermes`). No extra setup needed for non-default paths.

```bash
git clone https://github.com/Cheerwhy/hermes-lark-streaming.git
cd hermes-lark-streaming

# Install into Hermes's venv so the gateway can load the plugin
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m hermes_lark_streaming verify   # Verify compatibility
$HERMES_PYTHON -m hermes_lark_streaming install   # Inject hooks
hermes gateway restart
```

---

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
streaming:
  enabled: true
```

### Credentials

Credentials are resolved in the following order:

| Priority | Source | Variables |
|----------|--------|-----------|
| 1 | Environment | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` (or `LARK_APP_ID` / `LARK_APP_SECRET`) |
| 2 | Config file | `feishu` or `lark` section in `~/.hermes/config.yaml` |

```env
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

### Card Style

Customize the appearance of streaming and completion cards with the following options:

```yaml
streaming:
  enabled: true
  header:
    enabled: true      # Card header, default false
  body:
    text_size: normal_v2  # Answer body text size, default normal_v2
  footer:
    enabled: true         # Card footer, default true
    text_size: notation   # Footer text size, default notation
    fields:
      - [status, elapsed, context, model]
    show_label: false
  panel_expanded: false   # Keep completion panels expanded, default false
```

**Header** (`streaming.header.enabled`): Controls whether the card displays a status header bar. When enabled, the header auto-themes by state — blue for streaming, green for completed, red for stopped/error. Default: disabled.

**Footer** (`streaming.footer.enabled`): Controls whether the completion card displays a footer metadata bar. Default: enabled.

**Text Size** (`body.text_size` / `footer.text_size`): Valid values include `heading`, `normal`, `normal_v2`, `notation`, etc. See [Feishu docs](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/content-components/plain-text?lang=en-US).

**Footer Fields** (`footer.fields`): A 2D array where each sub-array is one line, fields joined by `·`.

| Field | Description | With Label | Without Label |
|-------|-------------|------------|---------------|
| `status` | Completion status | `✅ Completed` | `✅ Completed` |
| `elapsed` | Time elapsed | `Elapsed 12.3s` | `12.3s` |
| `model` | Model name | `deepseek-v4-flash` | `deepseek-v4-flash` |
| `tokens` | Token usage | `↑ 1.2K ↓ 500` | `↑ 1.2K ↓ 500` |
| `context` | Context window usage | `Context 50K/200K (25%)` | `50K/200K (25%)` |

**Show Label** (`footer.show_label`): Whether to display field labels like "Elapsed", "Context". Default: `false`.

**Panel Expand** (`panel_expanded`): Reasoning and tool panels are collapsed by default in completion cards. Set to `true` to keep them expanded.

---

## CLI Commands

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m hermes_lark_streaming verify     # Verify compatibility (no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore original files from backup
$HERMES_PYTHON -m hermes_lark_streaming status     # Show status
```

---

## Update

```bash
cd hermes-lark-streaming
git pull
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m pip install -e .
$HERMES_PYTHON -m hermes_lark_streaming uninstall   # Remove old injection first
$HERMES_PYTHON -m hermes_lark_streaming verify
$HERMES_PYTHON -m hermes_lark_streaming install
hermes gateway restart
```

---

## Uninstall

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m hermes_lark_streaming uninstall
$HERMES_PYTHON -m pip uninstall hermes-lark-streaming
```

---

## How It Works

The plugin injects hook calls into `gateway/run.py` and `cron/scheduler.py` via AST patching. All business logic lives in the `hermes_lark_streaming` package:

| Hook | Injection Target | Description |
|------|-----------------|-------------|
| `on_feishu_normalize` | After `source = event.source` in `_handle_message` | Fixes false thread_id on Feishu quoted messages |
| `on_message_started` | Top of `_handle_message_with_agent` | Creates card session and placeholder card |
| `on_tool_updated` | `progress_callback` | Displays tool call status in real-time |
| `on_answer_delta` | `_stream_delta_cb` | Streams answer text to the card |
| `on_thinking_delta` | `_interim_assistant_cb` | Displays reasoning/thinking process |
| `on_reasoning_delta` | After `agent.reasoning_config` assignment | Streams native model reasoning |
| `on_background_review_message` | At `background_review_callback` assignment | Defers self-evolution messages until card completion |
| `on_message_aborted` | Before stale `return None` | Handles `/stop` abort |
| `on_message_interrupted` | Before recursive `_run_agent` call | Handles message interrupts, terminates old card and creates new session |
| `on_queued_followup_boundary` | Before `was_interrupted = result.get("interrupted")` | Finalizes current card before Queue-mode follow-up drain |
| `on_queued_followup_result` | Before `return _preserve_queued_followup_history_offset` | Carries deepest completion ID back through recursive merge chain |
| `on_message_completed_wait` | Before `return response` | Waits for card creation/finalization before sending the completion card; yields to gateway text fallback on failure |
| `on_cron_deliver` | After `delivered = False` in `_deliver_result` | Intercepts Feishu cron delivery, sends as card |
| `on_background_deliver` | After `extract_images` in `_run_background_task` | Intercepts Feishu background task delivery, replies as card with topic-aware positioning |

**Message flow:**

```
User sends message
  → Card session created
  → Streaming updates (tool status, text — throttled)
  → Image URL async resolution
  → Completion card (tokens, duration, context)
```

If a message is deleted/recalled, UnavailableGuard auto-terminates further updates.

**Interrupt handling:**

- `/stop` abort — User actively stops generation, card shows interrupted state:

![](assets/abort.jpg)

- Message interrupt — User sends a new message while a response is in progress; old card shows interrupted state, and a new streaming card is automatically created for the new message:

![](assets/interrupt.jpg)

**Failure handling:**

| Scenario | Handling |
|----------|----------|
| CardKit streaming | 100ms throttled updates |
| CardKit creation failure | Gateway falls back to the default text reply |
| Rate limiting | Skips current frame, no channel switch |
| Completion failure | Gateway falls back to the default text reply |

---

## Notes

- `install` modifies `~/.hermes/hermes-agent/gateway/run.py` and `cron/scheduler.py`, and creates `.hermes_lark.bak` backups
- Re-run `verify` + `install` after Hermes updates
- The plugin complements the built-in Feishu adapter: plugin handles streaming cards, built-in adapter handles message routing
- Only affects Feishu/Lark platform — other platforms are unaffected

## Contributors

Thanks to our contributors for their issues and pull requests:

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/runfali"><img src="https://avatars.githubusercontent.com/u/39327978?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/thunderfight127-svg"><img src="https://avatars.githubusercontent.com/u/275854191?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/willggy"><img src="https://avatars.githubusercontent.com/u/74762604?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/atomperson"><img src="https://avatars.githubusercontent.com/u/14934637?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/linjunxin01"><img src="https://avatars.githubusercontent.com/u/63715504?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/mouxangithub"><img src="https://avatars.githubusercontent.com/u/48978046?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/numuly"><img src="https://avatars.githubusercontent.com/u/137970054?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/wzgrx"><img src="https://avatars.githubusercontent.com/u/39661556?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/zhaomingcheng01"><img src="https://avatars.githubusercontent.com/u/46734892?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

---

## License

[MIT](LICENSE)
