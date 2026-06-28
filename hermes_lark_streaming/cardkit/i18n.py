"""飞书卡片 i18n — 中英双语文本映射."""

from __future__ import annotations

__all__ = [
    "_LOCALES",
    "_T",
    "_i18n",
    "_t",
]

_LOCALES = ["zh_cn", "en_us"]

_T: dict[str, tuple[str, str]] = {
    "status_completed": ("✅ Completed", "✅ 已完成"),
    "status_error": ("❌ Error", "❌ 出错"),
    "status_stopped": ("🛑 Stopped", "🛑 已停止"),
    "elapsed": ("Elapsed {}", "耗时 {}"),
    "context": ("Context {}", "上下文 {}"),
    "processing": ("Processing...", "处理中..."),
    "processing_prefix": ("💭 Processing...", "💭 处理中..."),
    "tool_use": ("Tool use", "工具执行"),
    "tool_pending": ("🛠️ Tool use pending", "🛠️ 等待工具执行"),
    "steps": ("{} step{}", "{} 步"),
    "thought": ("Thought", "思考"),
    "thinking_panel": ("Thinking", "思考中"),
    "thought_for": ("Thought for {}", "思考了 {}"),
    "done": ("Done.", "完成。"),
    # interaction cards
    "interaction_reply_hint": (
        "Reply with the option number (e.g. 1, 2, 3).",
        "请回复数字选择（如 1、2、3）。",
    ),
    "interaction_clarify_header": ("❓ Clarification", "❓ 需要确认"),
    "interaction_approval_header": ("🔐 Approval Required", "🔐 需要授权"),
    "interaction_danger_header": ("⚠️ Approval Required", "⚠️ 需要授权"),
    "interaction_approve": ("✅ Approve", "✅ 同意"),
    "interaction_deny": ("❌ Deny", "❌ 拒绝"),
}


def _i18n(en: str, zh: str) -> dict[str, str]:
    return {"zh_cn": zh, "en_us": en}


def _t(key: str) -> dict[str, str]:
    """简写: _t("processing") → _i18n(*_T["processing"])。"""
    return _i18n(*_T[key])
