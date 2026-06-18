# Installation Guide — hermes-lark-streaming

A step-by-step guide to install the hermes-lark-streaming plugin into an existing
Hermes Agent deployment. Intended to be read and executed by an AI agent or a
human following the commands verbatim.

## Requirements

- Hermes Agent `>= 0.14.0` is installed and the `hermes` command is on `PATH`
  (`hermes --version` works).
- If `hermes` is not on `PATH`, stop — fix the Hermes installation first.

## Step 1 — Locate Hermes's Python

The plugin MUST be installed into Hermes's own venv (the gateway imports it at
runtime). Find that venv's Python interpreter:

```bash
# Read the venv path from the hermes CLI wrapper, then derive python3
HERMES_PYTHON=$(grep -oE 'exec "[^"]+"' "$(which hermes)" | sed 's/exec "//;s/"//')
HERMES_PYTHON=$(dirname "$HERMES_PYTHON")/python3

# Fallback (per-user default) if the command above yields nothing
[ -z "$HERMES_PYTHON" ] || [ ! -x "$HERMES_PYTHON" ] && HERMES_PYTHON=~/.hermes/hermes-agent/venv/bin/python3
```

Verify it exists and is Python 3.11+:

```bash
"$HERMES_PYTHON" --version
```

## Step 2 — Install the plugin into Hermes's venv

```bash
# Clone only if not already present (re-runs / updates skip this)
[ -d hermes-lark-streaming ] || git clone https://github.com/Cheerwhy/hermes-lark-streaming.git
cd hermes-lark-streaming
"$HERMES_PYTHON" -m pip install -e .
```

## Step 3 — Verify environment and compatibility

```bash
"$HERMES_PYTHON" -m hermes_lark_streaming status
"$HERMES_PYTHON" -m hermes_lark_streaming verify
```

`status` must show:

- `Hermes Python:` pointing to `$HERMES_PYTHON`
- `Hermes install dir:` pointing to the Hermes source tree
- No `warning:` line under `Hermes Python:`

`verify` must print `Compatible.` for both targets. If it reports
`Incompatible:`, the Hermes version is unsupported — do not proceed.

If a status warning appears, the CLI ran under the wrong interpreter — rerun
using the exact `$HERMES_PYTHON` path printed.

## Step 4 — Configure Feishu / Lark credentials

The plugin needs app credentials for Feishu (or Lark / Larksuite). Set them as
environment variables **persisted to Hermes's `.env`** (so the gateway sees them
after restart), or in `~/.hermes/config.yaml`:

```bash
# Option A — write to ~/.hermes/.env (read by the gateway on start)
cat >> ~/.hermes/.env <<'EOF'
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
EOF
chmod 600 ~/.hermes/.env
```

```yaml
# Option B — ~/.hermes/config.yaml
feishu:
  app_id: cli_xxxxx
  app_secret: xxxxx
```

For **Lark / Larksuite** (international), use the `lark` section and set the SDK
base URL:

```yaml
lark:
  app_id: cli_xxxxx
  app_secret: xxxxx
  base_url: https://open.larksuite.com
```

Also enable streaming in the same config:

```yaml
streaming:
  enabled: true
```

## Step 5 — Install the hooks

```bash
"$HERMES_PYTHON" -m hermes_lark_streaming install
```

This patches `gateway/run.py` and `cron/scheduler.py` in place. A `.hermes_lark.bak`
backup is created next to each file.

## Step 6 — Restart the gateway

```bash
hermes gateway restart
```

## Step 7 — Post-install verification

```bash
"$HERMES_PYTHON" -m hermes_lark_streaming status
```

All hooks should read `installed`, and `Feishu credentials:` should read
`configured`.

## Uninstall

```bash
"$HERMES_PYTHON" -m hermes_lark_streaming uninstall
"$HERMES_PYTHON" -m pip uninstall hermes-lark-streaming
```

## Update

```bash
cd hermes-lark-streaming
git pull
"$HERMES_PYTHON" -m pip install -e .
"$HERMES_PYTHON" -m hermes_lark_streaming uninstall   # remove old injection
"$HERMES_PYTHON" -m hermes_lark_streaming verify
"$HERMES_PYTHON" -m hermes_lark_streaming install
hermes gateway restart
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: hermes_lark_streaming` | Plugin not installed in the Python you used | Reinstall using the exact `HERMES_PYTHON` from Step 1 |
| `status` shows `warning: running under ...` | CLI invoked with wrong interpreter | Rerun with the `$HERMES_PYTHON` path shown in the warning |
| `verify` reports `Incompatible:` | Hermes version unsupported or changed anchors | Check Hermes version `>= 0.14.0`; wait for a plugin update |
| `gateway/run.py not found` | Hermes install layout not recognized | Run `cat "$(which hermes)"` to find the venv, then set `HERMES_PYTHON` manually |
| Credentials `MISSING` in `status` | Env vars / config not set, or not persisted | Complete Step 4; ensure credentials are in `~/.hermes/.env` or `config.yaml`, then restart the gateway |
| Gateway fails to load plugin after restart | Plugin installed into the wrong venv | Confirm `status` shows no warning before restarting |
