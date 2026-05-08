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
from typing import Any, Coroutine

from .cardkit import (
    STREAMING_ELEMENT_ID,
    build_complete_card,
    build_im_fallback_card,
    build_streaming_card,
    build_streaming_card_v2,
    optimize_markdown_style,
)
from .config import Config
from .feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_RATE_LIMITED,
    FeishuAPIError,
    FeishuClient,
    FeishuClientConfig,
)
from .flush import CARDKIT_MS, PATCH_MS, FlushController
from .image import ImageResolver
from .text import TextState, split_reasoning_text, strip_reasoning_tags
from .tooluse import ToolUseTracker
from .unavailable_guard import UnavailableGuard

_logger = logging.getLogger("hermes_lark_streaming")

IDLE = "idle"
CREATING = "creating"
STREAMING = "streaming"
COMPLETED = "completed"
FAILED = "failed"
ABORTED = "aborted"

_TERMINAL = {COMPLETED, FAILED, ABORTED}


class CardSession:
    """单条消息的卡片会话状态."""

    __slots__ = (
        "message_id", "chat_id", "state",
        "card_msg_id", "card_id", "use_cardkit",
        "text", "tool_use", "flush",
        "reasoning_text", "reasoning_start",
        "footer", "sequence",
        "_loop",
        "guard", "image_resolver",
        "last_tool_use_update", "created_at",
    )

    def __init__(
        self,
        message_id: str,
        chat_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.state = IDLE
        self.card_msg_id: str | None = None
        self.card_id: str | None = None
        self.use_cardkit: bool = False
        self.text = TextState()
        self.tool_use = ToolUseTracker()
        self.flush = FlushController(throttle_ms=PATCH_MS)
        self.reasoning_text = ""
        self.reasoning_start: float = 0.0
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


class StreamCardController:
    """流式卡片控制器 — 管理多条消息的卡片生命周期."""

    def __init__(self) -> None:
        self._cfg = Config()
        self._client: FeishuClient | None = None
        self._sessions: dict[str, CardSession] = {}
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
            self._client = FeishuClient(FeishuClientConfig(
                app_id=app_id,
                app_secret=app_secret,
                base_url=self._cfg.feishu_base_url,
            ))
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
        message_id: str,
        chat_id: str,
    ) -> None:
        """消息处理开始 — 创建会话 + 发占位卡片."""
        if not self.enabled:
            return
        if message_id in self._sessions:
            return

        self._prune_stale_sessions()

        loop = self._get_loop()
        if loop is None:
            _logger.warning("no event loop available, skipping: msg=%s", message_id[:12])
            return
        session = CardSession(message_id, chat_id, loop)
        self._sessions[message_id] = session
        _logger.info("session created: msg=%s chat=%s", message_id[:12], chat_id[:12])

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
            session.reasoning_text = split["reasoning_text"]
            if not session.reasoning_start:
                session.reasoning_start = time.time()
        elif split.get("answer_text"):
            if split.get("reasoning_text"):
                session.reasoning_text = split["reasoning_text"]
                if not session.reasoning_start:
                    session.reasoning_start = time.time()
            session.text.on_partial(split["answer_text"])

        self._schedule_card_update(session)

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

        if status in ("running", "started", "tool.started"):
            session.tool_use.record_start(tool_name, detail)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        self._schedule_card_update(session)

        if session.use_cardkit and session.card_id and not session.text.display_text:
            self._schedule_tool_use_status_update(session)

    def on_answer(self, *, message_id: str, text: str) -> None:
        """答案文本增量（流式）."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_answer"):
            return

        split = split_reasoning_text(text)
        if split.get("reasoning_text"):
            session.reasoning_text = split["reasoning_text"]
            if not session.reasoning_start:
                session.reasoning_start = time.time()

        answer_text = split.get("answer_text") or strip_reasoning_tags(text)
        if not answer_text:
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
            return False

        # 卡片创建失败 → 交回 gateway 正常回复
        if session.state == FAILED:
            _logger.info("on_completed: msg=%s state=FAILED, yielding to gateway", message_id[:12])
            self._cleanup(message_id)
            return False

        _logger.info(
            "on_completed: msg=%s has_card=%s state=%s use_cardkit=%s",
            message_id[:12], bool(session.card_msg_id), session.state, session.use_cardkit,
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

    def _schedule_card_update(self, session: CardSession) -> None:
        if session.state == IDLE or session.state in _TERMINAL:
            return
        if session.guard.should_skip("_schedule_card_update"):
            return

        session.flush.schedule_update(
            lambda: self._do_update_card(session)
        )

    def _schedule_tool_use_status_update(self, session: CardSession) -> None:
        if not session.use_cardkit or not session.card_id:
            return
        now = time.time()
        if now - session.last_tool_use_update < 1.5:
            return
        session.last_tool_use_update = now
        session.flush.schedule_update(
            lambda: self._do_tool_use_status_update(session)
        )

    async def _do_create_card(self, session: CardSession) -> None:
        if session.state != IDLE:
            return
        session.state = CREATING

        try:
            await self._ensure_init()
            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_card_update(session),
                )

            try:
                card = build_streaming_card_v2(show_tool_use=True)
                card_id = await self._client.cardkit_create(card)
                card_msg_id = await self._client.reply_card_by_id(
                    session.message_id, card_id,
                )
                session.card_id = card_id
                session.card_msg_id = card_msg_id
                session.use_cardkit = True
                session.flush.set_throttle(CARDKIT_MS)
            except FeishuAPIError:
                card = build_im_fallback_card()
                card_msg_id = await self._client.reply_card(
                    session.message_id, card,
                )
                session.card_msg_id = card_msg_id
                session.use_cardkit = False
                session.flush.set_throttle(PATCH_MS)

            session.flush.set_card_message_ready(True)
            if session.state == CREATING:
                session.state = STREAMING
            _logger.info(
                "card created: msg=%s cardkit=%s card_id=%s",
                session.message_id[:12], session.use_cardkit, (session.card_id or "")[:12],
            )
        except Exception:
            _logger.exception("_do_create_card failed")
            session.state = FAILED

    async def _do_update_card(self, session: CardSession) -> None:
        if session.state not in (CREATING, STREAMING):
            return
        if not session.card_msg_id:
            return
        if session.guard.should_skip("_do_update_card"):
            return

        display = session.text.display_text
        if not session.text.is_dirty(display):
            return

        if session.image_resolver:
            display = session.image_resolver.resolve_images(display)

        try:
            if session.use_cardkit and session.card_id:
                optimized = optimize_markdown_style(display)
                session.sequence += 1
                await self._client.cardkit_stream_element(
                    session.card_id,
                    STREAMING_ELEMENT_ID,
                    optimized or " ",
                    sequence=session.sequence,
                )
            else:
                tool_steps = session.tool_use.build_display_steps()
                card = build_streaming_card(
                    tool_steps=tool_steps,
                    reasoning_text=session.reasoning_text if not display else "",
                    text=display,
                    has_cardkit=False,
                )
                await self._client.update_card(session.card_msg_id, card)

            session.text.mark_flushed(display)
        except FeishuAPIError as e:
            if session.guard.terminate("_do_update_card", e):
                return

            if e.code == CARDKIT_RATE_LIMITED:
                _logger.info("rate limited, skipping frame")
                return

            if e.code == CARDKIT_CONTENT_FAILED:
                sub_code = e.extract_sub_code()
                if sub_code == CARDKIT_ELEMENT_LIMIT:
                    _logger.warning("card element limit exceeded, disabling CardKit streaming")
                    session.use_cardkit = False
                    session.flush.set_throttle(PATCH_MS)
                    return

            _logger.warning("card update failed: %s", e)

    async def _do_tool_use_status_update(self, session: CardSession) -> None:
        if not session.card_id or session.state in _TERMINAL:
            return
        try:
            tool_steps = session.tool_use.build_display_steps()
            card = build_streaming_card_v2(
                tool_steps=tool_steps,
                elapsed_ms=session.tool_use.elapsed_ms,
                show_tool_use=True,
            )
            session.sequence += 1
            await self._client.cardkit_update(
                session.card_id, card, sequence=session.sequence,
            )
        except Exception as e:
            _logger.debug("tool use status update failed: %s", e)

    async def _do_complete(self, session: CardSession) -> bool:
        try:
            return await self.__do_complete_inner(session)
        finally:
            self._cleanup(session.message_id)

    async def __do_complete_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete"):
            return False

        await session.flush.wait_for_flush()
        session.flush.mark_completed()

        display = session.text.display_text
        if session.image_resolver:
            try:
                display = await session.image_resolver.resolve_await(display)
            except Exception:
                _logger.debug("image resolve failed", exc_info=True)

        reasoning_elapsed_ms = 0.0
        if session.reasoning_start:
            reasoning_elapsed_ms = (time.time() - session.reasoning_start) * 1000

        is_error = session.state == FAILED
        is_aborted = session.state == ABORTED
        card = build_complete_card(
            text=display,
            reasoning_text=session.reasoning_text,
            reasoning_elapsed_ms=reasoning_elapsed_ms,
            tool_steps=session.tool_use.build_display_steps(),
            tool_elapsed_ms=session.tool_use.elapsed_ms,
            footer_data=session.footer,
            has_cardkit=session.use_cardkit,
            is_error=is_error,
            is_aborted=is_aborted,
            footer_fields=self._cfg.footer_fields,
            footer_show_label=self._cfg.footer_show_label,
        )

        for attempt in range(3):
            try:
                if session.use_cardkit and session.card_id:
                    await self._client.cardkit_close_streaming(
                        session.card_id, sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                    await self._client.cardkit_update(
                        session.card_id, card, sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                elif session.card_msg_id:
                    await self._client.update_card(session.card_msg_id, card)
                session.state = COMPLETED
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "cardkit complete attempt %d failed (FeishuAPIError): "
                    "code=%s msg=%s card_id=%s seq=%d",
                    attempt, e.code, e,
                    session.card_id, session.sequence,
                )
                if session.guard.terminate("_do_complete", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "cardkit complete attempt %d failed: %s: %s "
                    "card_id=%s card_msg_id=%s seq=%d",
                    attempt, type(e).__name__, e,
                    session.card_id, session.card_msg_id, session.sequence,
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                continue

        _logger.error(
            "cardkit complete failed after 3 attempts: card_id=%s card_msg_id=%s seq=%d",
            session.card_id, session.card_msg_id, session.sequence,
        )
        session.state = COMPLETED
        return False

    def _cleanup(self, message_id: str) -> None:
        session = self._sessions.pop(message_id, None)
        if session is None:
            return
        session.flush.mark_completed()
        if session.image_resolver:
            for task in session.image_resolver._pending.values():
                task.cancel()

    def _prune_stale_sessions(self) -> None:
        now = time.time()
        stale = [
            mid for mid, s in self._sessions.items()
            if now - s.created_at > self._session_ttl
        ]
        for mid in stale:
            _logger.warning("pruning stale session: msg=%s", mid[:12])
            self._cleanup(mid)

    @staticmethod
    def _on_bg_task_done(fut: asyncio.Future) -> None:
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
