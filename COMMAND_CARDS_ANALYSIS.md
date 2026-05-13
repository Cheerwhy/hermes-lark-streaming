# Analysis: Adding Card Status to Native Commands (/status, /help, etc.)

## Issue Request
User requested adding Feishu card UI to native Hermes commands like `/status`, `/help`, etc., similar to how agent responses get streaming cards.

## Current Architecture

### How Agent Responses Work
1. User sends a message
2. `_handle_message_with_agent()` is called
3. Plugin hooks inject at 7 points during agent execution:
   - `on_message_started` - Creates card session
   - `on_answer_delta` - Streams text updates
   - `on_tool_updated` - Shows tool execution
   - `on_message_completed` - Sends final card
   - etc.
4. Card is created and updated in real-time via Feishu CardKit API

### How Native Commands Work
1. User sends `/status` or `/help`
2. `_handle_message()` in run.py detects the command
3. Calls `_handle_status_command()` or `_handle_help_command()`
4. These functions return a plain text string
5. `base.py`'s `handle_message()` receives the string
6. Calls `adapter.send()` to send text message to Feishu

### The Gap
- Native commands **bypass all streaming hooks** because they don't go through `_handle_message_with_agent()`
- Commands complete before any agent processing starts
- Commands return platform-agnostic text, not platform-specific cards
- The response sending happens in `base.py` which the plugin doesn't patch

## Implementation Challenges

### Challenge 1: Multiple Injection Points
Each command has its own handler function:
- `_handle_status_command()` (line 8450)
- `_handle_help_command()` (line 8772)
- `_handle_commands_command()` (line 8797)
- `_handle_agents_command()` (line 8513)
- `_handle_model_command()` (line 8853)
- And many more...

To intercept all of them, we'd need to patch ~20+ different locations.

### Challenge 2: Response Path
The response flow is:
```
run.py: command_handler() -> returns text
  → base.py: handle_message() -> receives text
    → base.py: adapter.send() -> sends to platform
```

The plugin patches `run.py` but NOT `base.py`. We can't intercept the sending without patching `base.py`.

### Challenge 3: Platform Detection
At the command handler level, we have access to `event.source.platform`, so we CAN detect if it's Feishu. But we can't prevent `base.py` from also sending the text response without returning an empty string, which would suppress ALL platforms, not just Feishu.

### Challenge 4: Architectural Mismatch
- Commands are designed to be **instantaneous** and **platform-agnostic**
- The streaming card system is designed for **long-running agent responses**
- Commands don't need streaming - they just need a nice static card

## Possible Solutions

### Option 1: Patch Command Handlers Directly (Complex)
**Approach**: Inject code into each command handler to detect Feishu platform and send card.

**Pros**:
- Works within existing plugin architecture
- Doesn't require Hermes core changes

**Cons**:
- Requires ~20+ injection points (one per command)
- Duplicate response (card + text) unless we can suppress text
- Complex AST patching logic
- Fragile - breaks if Hermes renames/adds/removes commands

**Code Example**:
```python
async def _handle_status_command(self, event: MessageEvent) -> str:
    # ... original command logic ...
    result_text = "\n".join(lines)

    # INJECTED CODE:
    try:
        from hermes_lark_streaming.patch import on_command_response
        if on_command_response(
            command="status",
            response=result_text,
            event=event,
        ):
            return ""  # Card sent, suppress text
    except Exception:
        pass

    return result_text
```

### Option 2: Patch base.py Adapter Send (Not Feasible)
**Approach**: Patch `base.py` to intercept responses before sending.

**Cons**:
- `base.py` is part of platform adapters, not gateway/run.py
- Would require patching multiple files
- More fragile than current single-file patch

### Option 3: Hermes Plugin Hook (Cleanest, Requires Upstream)
**Approach**: Propose a new Hermes plugin hook: `post_gateway_command`

```python
# In base.py after command handler returns
_hook_results = invoke_hook(
    "post_gateway_command",
    command=command_name,
    response=response_text,
    event=event,
    adapter=self,
)
# Hook can return {"action": "override", "sent": True} to suppress default send
```

**Pros**:
- Clean architecture
- Extensible for all future use cases
- Minimal performance impact

**Cons**:
- Requires Hermes core changes
- Outside scope of this plugin

### Option 4: Feishu Adapter Subclass (Moderate Complexity)
**Approach**: Create a custom Feishu adapter that overrides `send()` to detect command responses.

**Pros**:
- No AST patching needed
- Centralized logic

**Cons**:
- Requires users to configure custom adapter in Hermes
- Still needs pattern matching to detect command output
- Fragile (command output format may change)

## Recommended Path Forward

### Short Term: Document Limitation
Add to README:

```markdown
## Limitations

- **Native Commands**: Commands like `/status`, `/help` currently send plain text responses, not interactive cards. This is due to architectural constraints in how Hermes handles commands vs. agent messages.

For command card support, we recommend:
1. Upvoting [Hermes issue #XXXX] for official command card support
2. Using custom slash commands that call the agent instead
```

### Medium Term: Minimal POC for High-Value Commands
Implement card support for ONLY `/status` and `/help` (the most commonly used) as a proof-of-concept:

1. Add 2 injection points (manageable)
2. Send card in addition to text (accept duplicate)
3. Document as experimental

### Long Term: Upstream Contribution
1. Propose `post_gateway_command` hook to Hermes
2. Once merged, implement full command card support in plugin
3. Clean, maintainable, extensible

## Technical Specification for POC

If we proceed with minimal POC for `/status` and `/help`:

### New Files/Changes

1. **hermes_lark_streaming/command_cards.py** - Card builders for commands
```python
def build_status_card(content: str) -> dict:
    """Build a static card for /status command output."""
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "📊 Session Status"},
            "template": "blue"
        },
        "elements": [{
            "tag": "markdown",
            "content": content
        }]
    }

def build_help_card(content: str) -> dict:
    """Build a static card for /help command output."""
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "ℹ️ Help"},
            "template": "green"
        },
        "elements": [{
            "tag": "markdown",
            "content": content
        }]
    }
```

2. **hermes_lark_streaming/patch.py** - Add command hook
```python
@_safe_hook(default_return=False)
def on_command_response(
    *,
    ctrl: Any,
    message_id: str,
    chat_id: str,
    command: str,
    response: str,
    platform: str,
) -> bool:
    """Send card for whitelisted commands on Feishu platform."""
    if platform != "feishu":
        return False

    return ctrl.on_command_response(
        message_id=message_id,
        chat_id=chat_id,
        command=command,
        response=response,
    )
```

3. **hermes_lark_streaming/patcher.py** - Add injection logic
```python
def _find_status_command_return(...) -> tuple[int, str] | None:
    """Find return statement in _handle_status_command."""
    # AST logic to find return point

def _find_help_command_return(...) -> tuple[int, str] | None:
    """Find return statement in _handle_help_command."""
    # AST logic to find return point
```

4. **hermes_lark_streaming/controller.py** - Add handler
```python
def on_command_response(self, message_id, chat_id, command, response):
    """Send a static card for supported commands."""
    # Build card based on command type
    # Send via FeishuClient
    # Return True if sent successfully
```

## Conclusion

Adding card support for native commands is architecturally challenging due to:
1. Commands bypass agent message hooks
2. Response sending happens in un-patched code
3. Platform-agnostic design doesn't fit card-specific UI

**Recommendation**: Document limitation and propose upstream hook for clean long-term solution.

**Alternative**: Implement minimal POC for `/status` and `/help` only, accepting some limitations (duplicate sends, limited command coverage).
