"""StreamCardController — 流式卡片主控制器（单例）.

与 openclaw-lark 对齐：
- UnavailableGuard 消息不可用保护
- 修复的 FlushController（wait_for_flush, card_message_ready）
- TextState 回复边界检测 + reasoning 处理
- ImageResolver 同步 strip + re-flush
- 工具状态预回答更新
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from concurrent.futures import Future as ConcurrentFuture
from typing import TYPE_CHECKING, Any

from .config import Config
from .controller_mixin import (
    _TERMINAL,
    ABORTED,
    FAILED,
    IDLE,
    L_ANSWER,
    L_IDLE,
    L_TOOL,
    STREAMING,
    ControllerMixin,
)
from .feishu import (
    FeishuClient,
    FeishuClientConfig,
)
from .flush import PATCH_MS, FlushController
from .text import TextState, split_reasoning_text, strip_reasoning_tags
from .tooluse import ToolUseTracker
from .unavailable_guard import UnavailableGuard

if TYPE_CHECKING:
    from .image import ImageResolver

_logger = logging.getLogger("hermes_lark_streaming")


class CardSession:
    """单条消息的卡片会话状态."""

    __slots__ = (
        "_linear_lock",
        "_linear_phase",
        "_loop",
        "_segment_start",
        "_tool_card_id",
        "_tool_card_msg_id",
        "_tool_start_idx",
        "card_id",
        "card_msg_id",
        "chat_id",
        "created_at",
        "flush",
        "footer",
        "guard",
        "image_resolver",
        "last_tool_use_update",
        "linear",
        "message_id",
        "reasoning_dirty",
        "reasoning_panel_added",
        "reasoning_start",
        "reasoning_text",
        "sequence",
        "state",
        "text",
        "tool_panel_added",
        "tool_use",
        "use_cardkit",
    )

    def __init__(
        self,
        message_id: str,
        chat_id: str,
        loop: asyncio.AbstractEventLoop,
        *,
        linear: bool = False,
    ) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.state = IDLE
        self.linear = linear
        self.card_msg_id: str | None = None
        self.card_id: str | None = None
        self.use_cardkit: bool = False
        self.text = TextState()
        self.tool_use = ToolUseTracker()
        self.flush = FlushController(throttle_ms=PATCH_MS)
        self.reasoning_text = ""
        self.reasoning_start: float = 0.0
        self.reasoning_dirty = False
        self.reasoning_panel_added = False
        self.footer: dict[str, Any] = {}
        self.sequence = 1
        self._loop = loop
        self.last_tool_use_update = 0.0
        self.created_at = time.time()

        self.guard = UnavailableGuard(
            reply_to_message_id=message_id,
            get_card_message_id=lambda: self.card_msg_id,
            on_terminate=lambda: setattr(self, "state", FAILED),
        )

        self.image_resolver: ImageResolver | None = None
        self.tool_panel_added = False

        self._linear_lock = asyncio.Lock()
        self._linear_phase = L_IDLE
        self._segment_start = 0
        self._tool_card_id: str | None = None
        self._tool_card_msg_id: str | None = None
        self._tool_start_idx = 0


class StreamCardController(ControllerMixin):
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self) -> None:
        self._cfg = Config()
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
        self._interrupt_map: dict[str, str] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._session_ttl = self._cfg.card_duration_sec
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled and bool(self._cfg.feishu_app_id or self._cfg.env_app_id)

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            app_id = self._cfg.feishu_app_id or self._cfg.env_app_id
            app_secret = self._cfg.feishu_app_secret or self._cfg.env_app_secret
            if not app_id or not app_secret:
                raise RuntimeError("feishu credentials not configured")
            self._client = FeishuClient(
                FeishuClientConfig(
                    app_id=app_id,
                    app_secret=app_secret,
                    base_url=self._cfg.feishu_base_url,
                )
            )
            self._initialized = True

    def _client_ok(self) -> bool:
        return self._initialized and self._client is not None

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        """获取事件循环，缓存以便跨线程复用."""
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            pass
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        try:
            loop = asyncio.get_event_loop()
            self._loop = loop
            return loop
        except RuntimeError:
            return None

    def _get_active_session(self, message_id: str) -> CardSession | None:
        """获取非终态的活跃 session，不存在或已终态返回 None."""
        session = self._sessions.get(message_id)
        if session is None or session.state in _TERMINAL:
            return None
        return session

    def _fire_and_forget(self, coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop) -> None:
        try:
            loop.create_task(coro)
        except RuntimeError:
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.add_done_callback(self._on_bg_task_done)
            except Exception:
                _logger.debug("fire_and_forget failed", exc_info=True)

    def on_message_started(
        self,
        *,
        message_id: str | None,
        chat_id: str,
    ) -> None:
        """消息处理开始 — 创建会话 + 发占位卡片."""
        if not self.enabled:
            return
        if not message_id:
            _logger.warning("on_message_started: missing message_id, chat=%s", chat_id[:12])
            return
        if message_id in self._sessions:
            return

        self._prune_stale_sessions()

        loop = self._get_loop()
        if loop is None:
            _logger.warning("no event loop available, skipping: msg=%s", message_id[:12])
            return
        linear = self._cfg.linear
        session = CardSession(message_id, chat_id, loop, linear=linear)
        self._sessions[message_id] = session
        _logger.info("session created: msg=%s chat=%s linear=%s", message_id[:12], chat_id[:12], linear)

        if linear:
            session.state = STREAMING
        else:
            self._fire_and_forget(self._do_create_card(session), loop)

    def on_thinking(self, *, message_id: str, text: str) -> None:
        """思考内容增量."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_thinking"):
            return

        split = split_reasoning_text(text)

        if split.get("reasoning_text") and not split.get("answer_text"):
            session.reasoning_text = split["reasoning_text"] or ""
            if not session.reasoning_start:
                session.reasoning_start = time.time()
        elif split.get("answer_text"):
            if split.get("reasoning_text"):
                session.reasoning_text = split["reasoning_text"] or ""
                if not session.reasoning_start:
                    session.reasoning_start = time.time()
            answer_text = split["answer_text"] or ""
            if answer_text:
                if session.linear and session._linear_phase in (L_IDLE, L_TOOL):
                    self._linear_enter_answer(session, answer_text)
                    return
                session.text.on_partial(answer_text)

        self._schedule_card_update(session)

    def on_reasoning(self, *, message_id: str, text: str) -> None:
        """Native model reasoning delta (incremental append)."""
        if not self.enabled:
            return
        if not self._cfg.show_reasoning:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_reasoning"):
            return

        if not session.reasoning_start:
            session.reasoning_start = time.time()
            _logger.info("reasoning started: msg=%s", message_id[:12])

        if session.linear:
            if session._linear_phase == L_IDLE:
                session._linear_phase = L_ANSWER
                self._fire_and_forget(self._do_create_answer_card(session), session._loop)
            elif session._linear_phase == L_ANSWER and session.use_cardkit and session.card_id:
                self._schedule_reasoning_update(session)
        else:
            if session.use_cardkit and session.card_id:
                self._schedule_reasoning_update(session)
            else:
                self._schedule_card_update(session)

        session.reasoning_text += text
        session.reasoning_dirty = True

    def on_tool_update(
        self,
        *,
        message_id: str,
        tool_name: str,
        status: str,
        detail: str = "",
    ) -> None:
        """工具调用事件."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_tool_update"):
            return

        is_started = status in ("running", "started", "tool.started")

        if is_started:
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        if session.linear:
            if is_started:
                if session._linear_phase == L_ANSWER:
                    session._linear_phase = L_TOOL
                    self._fire_and_forget(self._do_answer_to_tool(session), session._loop)
                elif session._linear_phase == L_TOOL:
                    self._fire_and_forget(self._do_update_tool_card(session), session._loop)
                elif session._linear_phase == L_IDLE:
                    session._linear_phase = L_TOOL
                    self._fire_and_forget(self._do_create_tool_card(session), session._loop)
            elif session._linear_phase == L_TOOL:
                self._fire_and_forget(self._do_update_tool_card(session), session._loop)
            return

        if session.use_cardkit and session.card_id:
            self._schedule_tool_use_status_update(session)
        else:
            self._schedule_card_update(session)

    def on_answer(self, *, message_id: str, text: str) -> None:
        """答案文本增量（流式）."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_answer"):
            return

        split = split_reasoning_text(text)
        if split.get("reasoning_text"):
            session.reasoning_text = split["reasoning_text"] or ""
            if not session.reasoning_start:
                session.reasoning_start = time.time()

        answer_text = split.get("answer_text") or strip_reasoning_tags(text)
        if not answer_text:
            return

        if session.linear:
            self._linear_answer(session, answer_text)
            return

        session.text.on_partial(answer_text)
        self._schedule_card_update(session)

    def on_aborted(self, *, message_id: str) -> None:
        """用户 /stop 导致消息被中断."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            return

        session.state = ABORTED
        session.flush.mark_completed()
        _logger.info("on_aborted: msg=%s state=ABORTED", message_id[:12])

        self._fire_and_forget(self._do_complete(session), session._loop)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B."""
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        if old_session is not None:
            old_session.state = ABORTED
            old_session.flush.mark_completed()
            _logger.info(
                "on_interrupted: abort old msg=%s",
                old_message_id[:12],
            )
            self._fire_and_forget(self._do_complete(old_session), old_session._loop)

        if new_message_id not in self._sessions:
            loop = self._get_loop()
            if loop is not None:
                linear = self._cfg.linear
                session = CardSession(new_message_id, chat_id, loop, linear=linear)
                self._sessions[new_message_id] = session
                _logger.info(
                    "on_interrupted: create new msg=%s chat=%s linear=%s",
                    new_message_id[:12],
                    chat_id[:12],
                    linear,
                )
                if linear:
                    session.state = STREAMING
                else:
                    self._fire_and_forget(self._do_create_card(session), loop)

        self._interrupt_map[old_message_id] = new_message_id
        for key, val in list(self._interrupt_map.items()):
            if val == old_message_id:
                self._interrupt_map[key] = new_message_id

    def on_completed(
        self,
        *,
        message_id: str,
        answer: str = "",
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
    ) -> bool:
        """消息处理完成 — 构建终端卡片."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None:
            redirected_id = self._interrupt_map.pop(message_id, None)
            if redirected_id is not None:
                _logger.info(
                    "on_completed: redirect msg=%s -> msg=%s",
                    message_id[:12],
                    redirected_id[:12],
                )
                session = self._get_active_session(redirected_id)
            if session is None:
                return False
            message_id = redirected_id or message_id

        # 卡片创建失败 → 交回 gateway 正常回复
        if session.state == FAILED:
            _logger.info("on_completed: msg=%s state=FAILED, yielding to gateway", message_id[:12])
            self._cleanup(message_id)
            return False

        _logger.info(
            "on_completed: msg=%s has_card=%s state=%s use_cardkit=%s",
            message_id[:12],
            bool(session.card_msg_id),
            session.state,
            session.use_cardkit,
        )

        if answer:
            session.text.on_deliver(answer)

        session.footer = {
            "duration": duration,
            "model": model,
            **({"input_tokens": tokens.get("input_tokens")} if tokens else {}),
            **({"output_tokens": tokens.get("output_tokens")} if tokens else {}),
            **({"context_used": context.get("used_tokens")} if context else {}),
            **({"context_max": context.get("max_tokens")} if context else {}),
        }

        self._fire_and_forget(self._do_complete(session), session._loop)
        return True

    def _linear_enter_answer(self, session: CardSession, answer_text: str) -> None:
        """线性模式：进入回答阶段，从 L_IDLE/L_TOOL 过渡到 L_ANSWER."""
        prev_phase = session._linear_phase
        session._segment_start = len(session.text.display_text)
        session.text.on_partial(answer_text)
        session._linear_phase = L_ANSWER
        if prev_phase == L_TOOL:
            self._fire_and_forget(self._do_tool_to_answer(session), session._loop)
        else:
            self._fire_and_forget(self._do_create_answer_card(session), session._loop)

    def _linear_answer(self, session: CardSession, answer_text: str) -> None:
        """线性模式 on_answer 路由."""
        if session._linear_phase in (L_IDLE, L_TOOL):
            self._linear_enter_answer(session, answer_text)
            return
        session.text.on_partial(answer_text)
        self._schedule_card_update(session)

    def _schedule_card_update(self, session: CardSession) -> None:
        if session.state == IDLE or session.state in _TERMINAL:
            return
        if session.guard.should_skip("_schedule_card_update"):
            return

        session.flush.schedule_update(lambda: self._do_update_card(session))

    def _schedule_tool_use_status_update(self, session: CardSession) -> None:
        if not session.use_cardkit or not session.card_id:
            return
        now = time.time()
        if now - session.last_tool_use_update < 1.5:
            return
        session.last_tool_use_update = now
        session.flush.schedule_update(lambda: self._do_tool_use_status_update(session))

    def _schedule_reasoning_update(self, session: CardSession) -> None:
        if not session.use_cardkit or not session.card_id:
            return
        if not session.reasoning_dirty:
            return
        session.flush.schedule_update(lambda: self._do_reasoning_update(session))

    def _cleanup(self, message_id: str) -> None:
        session = self._sessions.pop(message_id, None)
        if session is None:
            return
        stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
        for k in stale_keys:
            del self._interrupt_map[k]
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _prune_stale_sessions(self) -> None:
        now = time.time()
        stale = [mid for mid, s in self._sessions.items() if mid is not None and now - s.created_at > self._session_ttl]
        for mid in stale:
            _logger.warning("pruning stale session: msg=%s", mid[:12])
            self._cleanup(mid)

    @staticmethod
    def _on_bg_task_done(fut: ConcurrentFuture) -> None:
        try:
            fut.result()
        except Exception:
            _logger.warning("background task failed", exc_info=True)


_controller: StreamCardController | None = None


def get_controller() -> StreamCardController:
    global _controller
    if _controller is None:
        _controller = StreamCardController()
    return _controller
