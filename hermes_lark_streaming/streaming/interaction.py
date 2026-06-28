"""交互卡片 — detect Hermes clarify / approval messages and render them as Feishu CardKit interactive cards.

Architecture:
  on_message_started → _detect_interaction_message → if clarify/approval:
    → build_interaction_card → send_card_to_chat (standalone card)
    → signal Hermes to suppress plain-text delivery

Phase 2 (future): add button callbacks via card action handler.
"""

from __future__ import annotations

import re
from typing import Any

from ..cardkit.i18n import _T, _t, _i18n

# ── detection patterns ──────────────────────────────────────────────

# Hermes clarify text format: a question followed by numbered options
_CLARIFY_RE = re.compile(
    r"^(.+?)\n((?:\d+\.\s+.+(?:\n|$)){2,})",
    re.MULTILINE | re.DOTALL,
)

# Hermes approval text format: "Approve command execution?" + command
_APPROVAL_RE = re.compile(
    r"(approve|allow|confirm|proceed)\s+(?:this\s+)?(?:command|action|execution)",
    re.IGNORECASE,
)

# Danger indicators in approval text
_DANGER_KEYWORDS = [
    "rm -rf", "DROP TABLE", "DELETE FROM", "format", "dd if=",
    "shutdown", "reboot", "chmod 777", ":(){ :|:& };:",
]

# ── message classification ───────────────────────────────────────────

def classify_message(text: str) -> dict[str, Any] | None:
    """Determine if ``text`` is a clarify or approval message, and return parsed data."""
    if not text or not text.strip():
        return None

    stripped = text.strip()

    # 1. Try clarify pattern: "Question?\n1. option A\n2. option B"
    m = _CLARIFY_RE.match(stripped)
    if m:
        question = m.group(1).strip()
        options_block = m.group(2).strip()
        options = _parse_options(options_block)
        if len(options) >= 2:
            return {
                "type": "clarify",
                "question": question,
                "options": options,
                "is_danger": False,
            }

    # 2. Try approval pattern: contains "approve command" + danger keywords
    if _APPROVAL_RE.search(stripped):
        is_danger = any(kw.lower() in stripped.lower() for kw in _DANGER_KEYWORDS)
        return {
            "type": "approval",
            "question": stripped[:200],
            "options": [
                {"index": 1, "label": "Approve", "value": "approve"},
                {"index": 2, "label": "Deny", "value": "deny"},
            ],
            "is_danger": is_danger,
        }

    return None


def _parse_options(block: str) -> list[dict[str, Any]]:
    """Parse "1. Label\n2. Label\n..." lines into option dicts."""
    options = []
    for line in block.strip().split("\n"):
        line = line.strip()
        m = re.match(r"^(\d+)[\.\)]\s+(.+)$", line)
        if m:
            options.append({
                "index": int(m.group(1)),
                "label": m.group(2).strip(),
                "value": m.group(1),  # the number itself is the value for reply
            })
    return options


# ── card builder ──────────────────────────────────────────────────────

def build_interaction_card(
    *,
    interaction_type: str,
    question: str,
    options: list[dict[str, Any]],
    is_danger: bool = False,
) -> dict[str, Any]:
    """Build a CardKit v2.0 card for clarify or approval.

    Phase 1: visual-only card with numbered options. The user replies
    by typing the option number as a text message.

    Phase 2 (future): add ``behaviors.callback`` buttons to make the
    options truly interactive (requires a callback endpoint).
    """
    en_instruction, zh_instruction = _T.get("interaction_instruction",
        ("Please reply with the option number (e.g. 1, 2, 3).",
         "请回复数字选择（如 1、2、3）。"))

    header_template = "red" if is_danger else "blue"
    if interaction_type == "approval":
        en_title = "⚠️ Approval Required" if is_danger else "🔐 Approval Required"
        zh_title = "⚠️ 需要授权" if is_danger else "🔐 需要授权"
    else:
        en_title = "❓ Clarification"
        zh_title = "❓ 需要确认"

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"**{question}**",
            "text_size": "normal_v2",
            "margin": "0px 0px 12px 0px",
        },
    ]

    # Build option list
    option_lines_en: list[str] = []
    option_lines_zh: list[str] = []
    for opt in options:
        idx = opt["index"]
        label = opt["label"]
        option_lines_en.append(f"**{idx}.** {label}")
        option_lines_zh.append(f"**{idx}.** {label}")

    elements.append({
        "tag": "markdown",
        "content": "\n".join(option_lines_en),
        "i18n_content": _i18n("\n".join(option_lines_en), "\n".join(option_lines_zh)),
        "text_size": "normal_v2",
        "margin": "0px 0px 12px 0px",
    })

    # Instruction
    elements.append({
        "tag": "hr",
    })
    elements.append({
        "tag": "markdown",
        "content": en_instruction,
        "i18n_content": _i18n(en_instruction, zh_instruction),
        "text_size": "notation",
        "text_color": "grey",
    })

    card: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "locales": [{"locale": "en", "label": "English"}, {"locale": "zh", "label": "中文"}],
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": en_title,
                "i18n_content": _i18n(en_title, zh_title),
            },
            "template": header_template,
        },
        "body": {"elements": elements},
    }

    return card
