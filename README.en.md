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
- **Linear mode** — Dynamically renders thinking, tool calls, and answer elements in event arrival order within a single card
- **Reasoning display** — Shows model thinking/reasoning content
- **Tool use tracking** — Live tool call status with standard icons, result/error blocks
- **CardKit v2.0** — Prefers Feishu CardKit streaming API, auto-fallback to IM PATCH
- **Completion card** — Final card with token usage, duration, and context info
- **Message guard** — Auto-terminates updates when message is deleted/recalled
- **Image resolution** — Detects markdown image references, downloads and re-uploads as Feishu img_key
- **Abort handling** — Gracefully handles `/stop` command and message interrupts with aborted state card and automatic new session
- **i18n** — Built-in Chinese/English bilingual card text (status, tool panel, thinking labels, etc.) that auto-switches based on Feishu client language

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
  linear: true   # Optional: enable linear mode
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

### Footer

Customize the completion card footer via `streaming.footer`:

```yaml
streaming:
  enabled: true
  footer:
    fields:
      - [status, elapsed, context, model]
    show_label: false
```

**Fields** (`footer.fields`): A 2D array where each sub-array is one line, fields joined by `·`.

| Field | Description | With Label | Without Label |
|-------|-------------|------------|---------------|
| `status` | Completion status | `✅ Completed` | `✅ Completed` |
| `elapsed` | Time elapsed | `Elapsed 12.3s` | `12.3s` |
| `model` | Model name | `deepseek-v4-flash` | `deepseek-v4-flash` |
| `tokens` | Token usage | `↑ 1.2K ↓ 500` | `↑ 1.2K ↓ 500` |
| `context` | Context window usage | `Context 50K/200K (25%)` | `50K/200K (25%)` |

**Show Label** (`footer.show_label`): Whether to display field labels like "Elapsed", "Context". Default: `false`.

Default (when not configured): `fields: [[status, elapsed, context, model]]`, `show_label: false`.

### Linear Mode

When enabled, the plugin dynamically renders thinking, tool call, and answer elements in event arrival order. Reasoning and tool calls are no longer collapsed to the top — multi-round content is displayed in actual order.

When long conversations or excessive tool steps cause the card to approach Feishu's 200-element limit, it automatically splits into multiple cards: the old card is sealed with complete data, a new card continues streaming, and only the last card includes the footer. Oversized tool panels are also split at step boundaries.

```yaml
streaming:
  enabled: true
  linear: true
```

![](assets/linear.jpg)

---

## CLI Commands

```bash
HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
$HERMES_PYTHON -m hermes_lark_streaming verify     # Verify compatibility (no file changes)
$HERMES_PYTHON -m hermes_lark_streaming install    # Inject hooks
$HERMES_PYTHON -m hermes_lark_streaming uninstall  # Remove hooks
$HERMES_PYTHON -m hermes_lark_streaming restore    # Restore original run.py from backup
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

The plugin injects **9 hook calls** into `gateway/run.py` via AST patching. All business logic lives in the `hermes_lark_streaming` package:

| Hook | Injection Target | Description |
|------|-----------------|-------------|
| `on_message_started` | Top of `_handle_message_with_agent` | Creates card session and placeholder card |
| `on_tool_updated` | `progress_callback` | Displays tool call status in real-time |
| `on_answer_delta` | `_stream_delta_cb` | Streams answer text to the card |
| `on_thinking_delta` | `_interim_assistant_cb` | Displays reasoning/thinking process |
| `on_reasoning_delta` | After `agent.reasoning_config` assignment | Streams native model reasoning |
| `on_background_review_message` | At `background_review_callback` assignment | Defers self-evolution messages until card completion |
| `on_message_aborted` | Before stale `return None` | Handles `/stop` abort |
| `on_message_interrupted` | Before recursive `_run_agent` call | Handles message interrupts, terminates old card and creates new session |
| `on_message_completed` | Before `return response` | Sends completion card |

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

**Degradation strategy:**

| Strategy | Interval | Trigger |
|----------|----------|---------|
| CardKit streaming (preferred) | 100ms | Default |
| IM PATCH (fallback) | 1.5s | CardKit creation failure, table limit exceeded |
| Rate limiting | — | Skips current frame, no channel degradation |
| Completion failure | — | Gateway falls back to default text reply |

---

## Notes

- `install` modifies `~/.hermes/hermes-agent/gateway/run.py` and creates a `.hermes_lark.bak` backup
- Re-run `verify` + `install` after Hermes updates
- The plugin complements the built-in Feishu adapter: plugin handles streaming cards, built-in adapter handles message routing
- Only affects Feishu/Lark platform — other platforms are unaffected

## Contributors

Thanks to our contributors for their issues and pull requests:

<a href="https://github.com/Mxin-9527"><img src="https://avatars.githubusercontent.com/u/178271393?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/gitteeee"><img src="https://avatars.githubusercontent.com/u/128769493?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>
<a href="https://github.com/Bandersnatch0x"><img src="https://avatars.githubusercontent.com/u/13325067?v=4&s=64" width="48" height="48" style="border-radius:50%" /></a>

---

## License

[MIT](LICENSE)
