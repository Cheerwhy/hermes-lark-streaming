# Hermes Lark Streaming

[![Python](https://img.shields.io/badge/python-%E2%89%A53.11-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[中文](README.md)

![Hermes Lark Streaming](assets/cover.jpg)

Feishu/Lark CardKit 2.0 streaming cards for **Hermes >= 0.22.0**.

Version 0.14.0 uses Hermes's native platform registry and stream frame contract. It subclasses the built-in Feishu adapter and keeps the `feishu` platform identity, credentials, authorization, sessions, threads and cron targets. Everything runs inside the Gateway process: no source patches, AST rewriting, monkey-patching or sidecar.

The existing card renderer provides reasoning/tool panels, image handling and long-card splitting. Native hooks supply supplemental telemetry; native stream frames are the only source of answer text. The registered standalone sender supports cron deliveries without a running Gateway.

## Requirements

- Hermes **>= 0.22.0** with the built-in Feishu channel configured
- Python >= 3.11; Hermes manages its own runtime environment
- `lark-oapi >= 1.6.4`, `PyYAML >= 6.0`
- Feishu app permissions for sending/replying to messages, CardKit access and any media features used

Read the [migration guide](MIGRATION.md) before upgrading: remove old injections using the OLD release before installing this version.

## Installation

```bash
hermes plugins install https://github.com/Cheerwhy/hermes-lark-streaming
hermes plugins enable hermes-lark-streaming
hermes gateway restart
```

For local development, symlink the repository to `~/.hermes/plugins/hermes-lark-streaming`, then enable the plugin and restart as above. Restart the Gateway after source changes.

The plugin replaces the `feishu` presentation adapter through the native platform registry. Existing channel settings, credentials, sessions and cron targets stay in place.

## Configuration

Edit the active Hermes profile's `config.yaml` (default: `~/.hermes/config.yaml`). Merge these settings with your existing Feishu connection and access rules.

### Minimal setup

```yaml
streaming:
  enabled: true

platforms:
  feishu:
    enabled: true
    extra:
      card:
        enabled: true

display:
  platforms:
    feishu:
      tool_progress: 'off'
      show_tool_use: true
      show_reasoning: false
```

Hermes's master switch `streaming.enabled` defaults to `false` and must be enabled. Do not set `streaming.transport: off` or `display.platforms.feishu.streaming: false`.

### Card options

All keys below are relative to `platforms.feishu.extra.card`. Unset values use these defaults. Appearance options apply to streaming cards and their final state; standalone cron and background deliveries use a simple fixed layout.

| Key | Default | Effect |
| --- | --- | --- |
| `enabled` | `true` | Enable native streaming cards; `false` disables streaming integration, not the entire plugin |
| `width_mode` | `default` | `default`, `compact` or `fill`; invalid values fall back to `default` |
| `panel_expanded` | `false` | Keep reasoning and tool panels expanded after completion; otherwise collapse them |
| `header.enabled` | `false` | Show the status header |
| `body.text_size` | `normal_v2` | Answer Markdown font size; use a CardKit-supported size token |
| `footer.enabled` | `true` | Show the final footer |
| `footer.text_size` | `notation` | Footer font size; use a CardKit-supported size token |
| `footer.fields` | `[[status, elapsed, context, model]]` | Footer fields and ordering; see below |
| `footer.show_label` | `false` | Add labels to duration and context |

For example, merge this appearance configuration under your existing `card`:

```yaml
width_mode: fill
panel_expanded: true
header:
  enabled: true
body:
  text_size: normal_v2
```

### Footer layout

Each inner array in `footer.fields` defines one row; fields within a row are separated by `·`. The following five fields are supported. Fields without data are omitted.

| Field | Content |
| --- | --- |
| `status` | Completed, error or stopped |
| `elapsed` | Card duration |
| `context` | Latest request's full prompt / host context limit and percentage, including cached input |
| `tokens` | Accumulated usage for this turn: `↑` uncached input, `↓` output; full input is used if that is all the host provides |
| `model` | Model name |

For a two-row footer, set this under `platforms.feishu.extra.card`:

```yaml
footer:
  enabled: true
  text_size: notation
  show_label: true
  fields:
    - [status, elapsed]
    - [tokens, model]
```

A flat array such as `[status, elapsed]` creates one row. An empty array restores the default layout; use `footer.enabled: false` to hide the footer. `context` is omitted if the prompt count or context limit is unavailable. The default is one row of `[status, elapsed, context, model]`; add `tokens` as needed.

### Reasoning and tools

| Configuration path | Default | Effect |
| --- | --- | --- |
| `display.platforms.feishu.show_tool_use` | `true` | Show tool names, short arguments, status and duration; omit full successful results |
| `display.platforms.feishu.show_reasoning` | `false` | Show reasoning provided by the model; also enable the event switch below |
| `plugins.stream_reasoning_deltas` | `false` | Allow Hermes to send streaming reasoning events to plugins |
| `display.platforms.feishu.tool_progress` | Set by Hermes | Set to `'off'` to avoid duplicating the plugin's tool panel with host progress text |

For `show_tool_use` and `show_reasoning`, precedence is Feishu-specific setting → the same key under global `display` → the default above. To display reasoning:

```yaml
plugins:
  stream_reasoning_deltas: true
display:
  platforms:
    feishu:
      show_reasoning: true
```

Display switches do not restrict tool permissions or make a model provide reasoning it does not emit.

### Applying changes and channel settings

Run `hermes gateway restart` after changing card options or host streaming settings. `show_tool_use` and `show_reasoning` are reread at runtime for subsequent rendering; completed historical cards are not proactively refreshed.

Keep using Hermes's built-in Feishu credentials (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_DOMAIN`) and connection/group settings under `platforms.feishu.extra`. Do not place them under `card`. This plugin adds no channel permission settings.

Legacy card options move from `streaming.*` to `platforms.feishu.extra.card`; the global `streaming.enabled` stays in place. Configuration is not automatically migrated; see the [migration guide](MIGRATION.md).

Approval follow-ups are delivered as final static cards under the Hermes 0.22.0 protocol; clarification can reopen streaming. Clarification uses the built-in text reply flow, and approval buttons retain built-in callbacks.

## Card Preview

Streaming answers with reasoning and tool panels; long content automatically splits across cards.

![Streaming card](assets/streaming.jpg)

Stopping with `/stop`:

![Stopped card](assets/abort.jpg)

Interrupting an active response with a new message:

![Interrupted response](assets/interrupt.jpg)

Screenshots show the existing renderer; appearance depends on card settings and the host version. Native integration has passed automated tests; live Feishu acceptance for 0.14.0 is pending.

## Disabling and diagnostics

```bash
hermes plugins disable hermes-lark-streaming
hermes gateway restart
```

Disabling restores built-in Feishu rendering. `python -m hermes_lark_streaming verify` checks local native interfaces and leftover injections without sending messages or changing configuration. The old patch commands `install`, `uninstall` and `restore` have been removed.

## Development and validation

Install the plugin and a fixed Hermes 0.22.0 checkout in an isolated development environment, then run:

```bash
python -m pip install -e '.[dev]'
python -m ruff check hermes_lark_streaming tests scripts __init__.py
python -m mypy hermes_lark_streaming scripts
python -m pytest tests -q
```

The plugin reuses the built-in Feishu adapter and host message context, including private interfaces. Recheck compatibility after upgrading Hermes.

CI covers Hermes 0.22.0 and 0.22.5 with mocked Feishu APIs and real Hermes registries and stream consumers. Live Feishu messaging requires separate acceptance testing.

For real outbound checks, set `FEISHU_E2E_APP_ID` and `FEISHU_E2E_APP_SECRET`, then run `python scripts/feishu_smoke.py --chat-id <test-chat-id> --scenario all`. It sends fixed test cards for streaming, sequence recovery, splitting, stopping and static updates; add `--reply-to <message-id>` to check quoted replies. No model or tools run. Inbound permissions, clarification and Cron scheduling still need manual acceptance.

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

Inspired by [openclaw-lark](https://github.com/larksuite/openclaw-lark) and [hermes-feishu-streaming-card](https://github.com/baileyh8/hermes-feishu-streaming-card).

## License

[MIT](LICENSE)
