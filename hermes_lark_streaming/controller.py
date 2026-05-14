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
from typing import Any

from .cardkit import (
    STREAMING_ELEMENT_ID,
    TOOL_PANEL_ELEMENT_ID,
    _build_tool_panel,
    _downgrade_tables,
    build_complete_card,
    build_im_fallback_card,
    build_round_tool_card,
    build_segment_card,
    build_streaming_card,
    build_streaming_card_v2,
    optimize_markdown_style,
)
from .config import Config
from .feishu import (
    CARDKIT_CONTENT_FAILED,
    CARDKIT_ELEMENT_LIMIT,
    CARDKIT_RATE_LIMITED,
    CARDKIT_SEQUENCE_CONFLICT,
    CARDKIT_STREAMING_CLOSED,
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
        "_answer_card_pending",
        "_answer_card_ready",
        "_loop",
        "_round_tool_card_id",
        "_round_tool_card_msg_id",
        "_round_tool_card_pending",
        "_round_tool_card_ready",
        "_round_tool_card_sequence",
        "_round_tool_card_use_cardkit",
        "_round_tool_start_idx",
        "_segment_lock",
        "_segment_start",
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
        self._answer_card_pending = False
        self._answer_card_ready = asyncio.Event()
        self._segment_start = 0
        self._segment_lock = asyncio.Lock()

        self._round_tool_card_id: str | None = None
        self._round_tool_card_msg_id: str | None = None
        self._round_tool_card_use_cardkit = False
        self._round_tool_card_sequence = 0
        self._round_tool_card_pending = False
        self._round_tool_card_ready = asyncio.Event()
        self._round_tool_start_idx = 0


class StreamCardController:
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
            session.text.on_partial(split["answer_text"] or "")

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
            if session.linear and session.tool_use._session is not None:
                if session._round_tool_card_id or session._round_tool_card_pending:
                    self._fire_and_forget(
                        self._do_update_round_tool_card(session), session._loop
                    )
                else:
                    session._round_tool_card_pending = True
                    self._fire_and_forget(
                        self._do_segment_tool_transition(session),
                        session._loop,
                    )
                return
        else:
            is_error = status in ("error", "failed")
            ended_idx = session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )
            if session.linear and ended_idx is not None and session.tool_use._session is not None:
                if session._round_tool_card_id or session._round_tool_card_pending:
                    self._fire_and_forget(
                        self._do_update_round_tool_card(session), session._loop
                    )
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

        need_create = session.linear and not session.card_id and not session._answer_card_pending
        if need_create:
            session._segment_start = len(session.text.display_text)
            session._answer_card_pending = True

        session.text.on_partial(answer_text)

        if need_create:
            if session._round_tool_card_id or session._round_tool_card_pending:
                self._fire_and_forget(self._do_answer_transition(session), session._loop)
            else:
                self._fire_and_forget(self._do_create_answer_card(session), session._loop)
            return

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

    async def _do_create_card(self, session: CardSession) -> None:
        if session.state != IDLE:
            return
        session.state = CREATING

        try:
            await self._ensure_init()
            assert self._client is not None
            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_card_update(session),
                )

            await self._create_and_reply_card(session)

            session.flush.set_card_message_ready(True)
            if session.state == CREATING:
                session.state = STREAMING
            _logger.info(
                "card created: msg=%s cardkit=%s card_id=%s",
                session.message_id[:12],
                session.use_cardkit,
                (session.card_id or "")[:12],
            )
        except Exception:
            _logger.exception("_do_create_card failed")
            session.state = FAILED

    async def _create_and_reply_card(self, session: CardSession) -> None:
        """创建 CardKit 实体 + 回复用户消息（CardKit 优先，IM fallback）."""
        assert self._client is not None
        try:
            card = build_streaming_card_v2(show_tool_use=False)
            card_id = await self._client.cardkit_create(card)
            card_msg_id = await self._client.reply_card_by_id(session.message_id, card_id)
            session.card_id = card_id
            session.card_msg_id = card_msg_id
            session.use_cardkit = True
            session.flush.set_throttle(CARDKIT_MS)
        except FeishuAPIError:
            card = build_im_fallback_card()
            card_msg_id = await self._client.reply_card(session.message_id, card)
            session.card_msg_id = card_msg_id
            session.use_cardkit = False
            session.flush.set_throttle(PATCH_MS)

    async def _do_update_card(self, session: CardSession) -> None:
        if session.state not in (CREATING, STREAMING):
            return
        if not session.card_msg_id:
            return
        if session.guard.should_skip("_do_update_card"):
            return

        full_display = session.text.display_text
        if not session.text.is_dirty(full_display):
            _logger.info(
                "update_card skipped (not dirty): msg=%s len=%d",
                session.message_id[:12],
                len(full_display),
            )
            return

        display = full_display[session._segment_start:] if session.linear else full_display

        if session.image_resolver:
            display = session.image_resolver.resolve_images(display)

        _logger.info(
            "update_card: msg=%s seq=%d len=%d cardkit=%s",
            session.message_id[:12],
            session.sequence + 1,
            len(display),
            session.use_cardkit,
        )

        try:
            assert self._client is not None
            if session.use_cardkit and session.card_id:
                optimized = _downgrade_tables(optimize_markdown_style(display))
                session.sequence += 1
                await self._client.cardkit_stream_element(
                    session.card_id,
                    STREAMING_ELEMENT_ID,
                    optimized or " ",
                    sequence=session.sequence,
                )
            else:
                tool_steps = [] if session.linear else session.tool_use.build_display_steps()
                card = build_streaming_card(
                    tool_steps=tool_steps,
                    reasoning_text=session.reasoning_text if not display else "",
                    text=display,
                )
                await self._client.update_card(session.card_msg_id, card)

            session.text.mark_flushed(full_display)
        except FeishuAPIError as e:
            if session.guard.terminate("_do_update_card", e):
                return

            if e.code == CARDKIT_RATE_LIMITED:
                _logger.info("rate limited, skipping frame")
                return

            if e.code == CARDKIT_STREAMING_CLOSED:
                _logger.info("streaming mode closed, skipping update: msg=%s", session.message_id[:12])
                return

            if e.code == CARDKIT_CONTENT_FAILED:
                sub_code = e.extract_sub_code()
                if sub_code == CARDKIT_ELEMENT_LIMIT:
                    _logger.warning("card element limit exceeded, disabling CardKit streaming")
                    session.use_cardkit = False
                    session.flush.set_throttle(PATCH_MS)
                    return

            _logger.warning("card update failed: %s", e, exc_info=True)

    async def _do_tool_use_status_update(self, session: CardSession) -> None:
        if not session.card_id or session.state in _TERMINAL:
            return
        try:
            assert self._client is not None
            tool_steps = session.tool_use.build_display_steps()
            panel = _build_tool_panel(
                tool_steps,
                session.tool_use.elapsed_ms,
            )
            if not session.tool_panel_added:
                actions = [
                    {
                        "action": "add_elements",
                        "params": {
                            "type": "insert_before",
                            "target_element_id": STREAMING_ELEMENT_ID,
                            "elements": [panel],
                        },
                    }
                ]
            else:
                actions = [
                    {
                        "action": "update_element",
                        "params": {
                            "element_id": TOOL_PANEL_ELEMENT_ID,
                            "element": panel,
                        },
                    }
                ]
            session.sequence += 1
            _logger.info(
                "tool_update: msg=%s seq=%d action=%s steps=%d",
                session.message_id[:12],
                session.sequence,
                "add" if not session.tool_panel_added else "update",
                len(tool_steps),
            )
            await self._client.cardkit_batch_update(
                session.card_id,
                actions,
                sequence=session.sequence,
            )
            session.tool_panel_added = True
        except Exception as e:
            _logger.debug("tool use status update failed: %s", e, exc_info=True)

    async def _do_close_answer_card(self, session: CardSession) -> None:
        """线性模式：关闭当前回答卡片（段间分界），用已累积文本做最终更新."""
        if not session.card_id:
            return
        card_id = session.card_id
        card_msg_id = session.card_msg_id
        use_cardkit = session.use_cardkit

        full_display = session.text.display_text
        segment_text = full_display[session._segment_start:]
        if session.image_resolver:
            segment_text = session.image_resolver.resolve_images(segment_text)

        try:
            assert self._client is not None
            if use_cardkit and card_id:
                card = build_segment_card(segment_text, has_cardkit=True)
                session.sequence += 1
                await self._client.cardkit_close_streaming(card_id, sequence=session.sequence)
                session.sequence += 1
                await self._client.cardkit_update(card_id=card_id, card=card, sequence=session.sequence)
            elif card_msg_id:
                card = build_segment_card(segment_text, has_cardkit=False)
                await self._client.update_card(card_msg_id, card)
            _logger.info(
                "segment answer card closed: msg=%s card_id=%s len=%d",
                session.message_id[:12],
                (card_id or card_msg_id or "")[:12],
                len(segment_text),
            )
        except Exception:
            _logger.exception("_do_close_answer_card failed")
            # Best-effort: try to close streaming mode so the card doesn't
            # stay stuck in a perpetual loading state on the server side.
            if use_cardkit and card_id and self._client:
                try:
                    session.sequence += 1
                    await self._client.cardkit_close_streaming(card_id, sequence=session.sequence)
                except Exception:
                    _logger.debug("best-effort close_streaming also failed", exc_info=True)
            session.card_id = None
            session.card_msg_id = None
            session.use_cardkit = False
            session.flush.set_card_message_ready(False)
            return

        session.card_id = None
        session.card_msg_id = None
        session.use_cardkit = False
        session.flush.set_card_message_ready(False)

    async def _do_segment_tool_transition(self, session: CardSession) -> None:
        """线性模式：回答→工具过渡 — 关闭回答卡，创建轮次工具卡."""
        async with session._segment_lock:
            if session._answer_card_pending:
                try:
                    await asyncio.wait_for(session._answer_card_ready.wait(), timeout=5.0)
                except TimeoutError:
                    _logger.warning("segment transition: answer card creation timed out")
                session._answer_card_ready.clear()
            if session.card_id:
                await self._do_close_answer_card(session)
            await self._do_create_round_tool_card(session)

    async def _do_answer_transition(self, session: CardSession) -> None:
        """线性模式：工具→回答过渡 — 关闭轮次工具卡，创建回答卡."""
        async with session._segment_lock:
            if session._round_tool_card_pending and not session._round_tool_card_id:
                try:
                    await asyncio.wait_for(session._round_tool_card_ready.wait(), timeout=5.0)
                except TimeoutError:
                    _logger.warning("answer transition: round tool card creation timed out")
                session._round_tool_card_ready.clear()
            if session._round_tool_card_id:
                await self._do_close_round_tool_card(session)
            await self._do_create_answer_card(session)

    async def _do_create_round_tool_card(self, session: CardSession) -> None:
        """线性模式：为一轮工具调用创建共享卡片."""
        display_steps = session.tool_use.build_display_steps(session._round_tool_start_idx)
        if not display_steps:
            session._round_tool_card_pending = False
            session._round_tool_card_ready.set()
            return

        try:
            await self._ensure_init()
            assert self._client is not None

            card = build_round_tool_card(display_steps, session.tool_use.elapsed_ms)
            card_id = await self._client.cardkit_create(card)
            card_msg_id = await self._client.send_card_by_id(session.chat_id, card_id)

            session._round_tool_card_id = card_id
            session._round_tool_card_msg_id = card_msg_id
            session._round_tool_card_use_cardkit = True
            session._round_tool_card_sequence = 1

            _logger.info(
                "round tool card created: msg=%s card_id=%s steps=%d",
                session.message_id[:12],
                card_id[:12],
                len(display_steps),
            )
        except Exception:
            _logger.exception("_do_create_round_tool_card failed")
        finally:
            session._round_tool_card_pending = False
            session._round_tool_card_ready.set()

    async def _do_update_round_tool_card(self, session: CardSession) -> None:
        """线性模式：更新轮次工具卡（新增/完成工具）."""
        if session._round_tool_card_pending and not session._round_tool_card_id:
            try:
                await asyncio.wait_for(session._round_tool_card_ready.wait(), timeout=5.0)
            except TimeoutError:
                _logger.warning("round tool card update: creation timed out")
                return
            session._round_tool_card_ready.clear()

        if not session._round_tool_card_id:
            return

        display_steps = session.tool_use.build_display_steps(session._round_tool_start_idx)
        if not display_steps:
            return

        try:
            assert self._client is not None
            card = build_round_tool_card(display_steps, session.tool_use.elapsed_ms)
            session._round_tool_card_sequence += 1
            await self._client.cardkit_update(
                card_id=session._round_tool_card_id,
                card=card,
                sequence=session._round_tool_card_sequence,
            )
            _logger.info(
                "round tool card updated: msg=%s steps=%d seq=%d",
                session.message_id[:12],
                len(display_steps),
                session._round_tool_card_sequence,
            )
        except FeishuAPIError as e:
            if e.code == CARDKIT_SEQUENCE_CONFLICT:
                _logger.info("round tool card update: sequence conflict, skipping")
                return
            _logger.warning("round tool card update failed: %s", e)
        except Exception:
            _logger.exception("_do_update_round_tool_card failed")

    async def _do_close_round_tool_card(self, session: CardSession) -> None:
        """线性模式：关闭轮次工具卡（最终更新后清除状态）."""
        if not session._round_tool_card_id:
            return

        display_steps = session.tool_use.build_display_steps(session._round_tool_start_idx)
        if display_steps:
            try:
                assert self._client is not None
                card = build_round_tool_card(
                    display_steps, session.tool_use.elapsed_ms, expanded=True
                )
                session._round_tool_card_sequence += 1
                await self._client.cardkit_update(
                    card_id=session._round_tool_card_id,
                    card=card,
                    sequence=session._round_tool_card_sequence,
                )
            except Exception:
                _logger.debug("round tool card close update failed", exc_info=True)

        if session.tool_use._session is not None:
            session._round_tool_start_idx = len(session.tool_use._session.steps)

        session._round_tool_card_id = None
        session._round_tool_card_msg_id = None
        session._round_tool_card_use_cardkit = False

    async def _do_create_answer_card(self, session: CardSession) -> None:
        """线性模式：为回答文本创建流式卡片并回复用户消息."""
        if session.card_id:
            session._answer_card_pending = False
            session._answer_card_ready.set()
            return

        try:
            await self._ensure_init()
            assert self._client is not None

            if session.image_resolver is None and self._client:
                session.image_resolver = ImageResolver(
                    client=self._client,
                    on_image_resolved=lambda: self._schedule_card_update(session),
                )

            await self._create_and_reply_card(session)

            session.flush.set_card_message_ready(True)
            session.state = STREAMING
            session._answer_card_pending = False
            session._answer_card_ready.set()
            _logger.info(
                "linear answer card created: msg=%s cardkit=%s card_id=%s",
                session.message_id[:12],
                session.use_cardkit,
                (session.card_id or "")[:12],
            )
        except Exception:
            _logger.exception("_do_create_answer_card failed")
            session._answer_card_pending = False
            session._answer_card_ready.set()
            session.state = FAILED
            return

        try:
            display = session.text.display_text[session._segment_start:]
            if session.use_cardkit and session.card_id and display:
                session.sequence += 1
                optimized = _downgrade_tables(optimize_markdown_style(display))
                await self._client.cardkit_stream_element(
                    session.card_id,
                    STREAMING_ELEMENT_ID,
                    optimized or " ",
                    sequence=session.sequence,
                )
                session.text.mark_flushed(session.text.display_text)
                _logger.info(
                    "update_card: msg=%s seq=%d len=%d cardkit=True",
                    session.message_id[:12],
                    session.sequence,
                    len(display),
                )
            elif not session.use_cardkit and session.card_msg_id and display:
                card = build_streaming_card(text=display)
                await self._client.update_card(session.card_msg_id, card)
                session.text.mark_flushed(session.text.display_text)
                _logger.info(
                    "update_card: msg=%s seq=%d len=%d cardkit=False",
                    session.message_id[:12],
                    0,
                    len(display),
                )
        except Exception:
            _logger.debug("initial answer card flush failed", exc_info=True)

    async def _do_complete(self, session: CardSession) -> bool:
        try:
            return await self.__do_complete_inner(session)
        finally:
            self._cleanup(session.message_id)

    async def __do_complete_inner(self, session: CardSession) -> bool:
        if session.guard.should_skip("_do_complete"):
            return False

        await session.flush.wait_for_flush()

        if session._answer_card_pending:
            try:
                await asyncio.wait_for(session._answer_card_ready.wait(), timeout=5.0)
            except TimeoutError:
                _logger.warning("complete: answer card creation timed out")
            session._answer_card_ready.clear()

        session.flush.mark_completed()

        if session.linear:
            if session._round_tool_card_pending and not session._round_tool_card_id:
                try:
                    await asyncio.wait_for(session._round_tool_card_ready.wait(), timeout=5.0)
                except TimeoutError:
                    _logger.warning("complete: round tool card creation timed out")
                session._round_tool_card_ready.clear()
            if session._round_tool_card_id:
                await self._do_close_round_tool_card(session)

        display = session.text.display_text
        _logger.info(
            "do_complete: msg=%s state=%s display_len=%d cardkit=%s seq=%d",
            session.message_id[:12],
            session.state,
            len(display),
            session.use_cardkit,
            session.sequence,
        )
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

        if session.linear and not session.card_id:
            session.state = COMPLETED
            return True

        if session.linear:
            display = display[session._segment_start:]

        tool_steps = [] if session.linear else session.tool_use.build_display_steps()
        card = build_complete_card(
            text=display,
            reasoning_text=session.reasoning_text,
            reasoning_elapsed_ms=reasoning_elapsed_ms,
            tool_steps=tool_steps,
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
                assert self._client is not None
                if session.use_cardkit and session.card_id:
                    await self._client.cardkit_close_streaming(
                        session.card_id,
                        sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                    await self._client.cardkit_update(
                        session.card_id,
                        card,
                        sequence=session.sequence + 1,
                    )
                    session.sequence += 1
                elif session.card_msg_id:
                    await self._client.update_card(session.card_msg_id, card)
                session.state = COMPLETED
                return True
            except FeishuAPIError as e:
                _logger.warning(
                    "cardkit complete attempt %d failed (FeishuAPIError): code=%s msg=%s card_id=%s seq=%d",
                    attempt,
                    e.code,
                    e,
                    session.card_id,
                    session.sequence,
                    exc_info=True,
                )
                if session.guard.terminate("_do_complete", e):
                    return False
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue
            except Exception as e:
                _logger.warning(
                    "cardkit complete attempt %d failed: %s: %s card_id=%s card_msg_id=%s seq=%d",
                    attempt,
                    type(e).__name__,
                    e,
                    session.card_id,
                    session.card_msg_id,
                    session.sequence,
                    exc_info=True,
                )
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
                continue

        _logger.error(
            "cardkit complete failed after 3 attempts: card_id=%s card_msg_id=%s seq=%d",
            session.card_id,
            session.card_msg_id,
            session.sequence,
        )
        session.state = FAILED
        return False

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
