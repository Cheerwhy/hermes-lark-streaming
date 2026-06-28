"""StreamCardController — 流式卡片主控制器（单例）."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future as ConcurrentFuture
from typing import Any

from .config import Config
from .feishu import (
    FeishuClient,
    FeishuClientConfig,
)
from .streaming.controller import StreamingController
from .streaming.interaction import build_interaction_card, classify_message
from .streaming.segments import SegmentType
from .streaming.session import CardSession, SessionState
from .streaming.text import strip_reasoning_tags

_logger = logging.getLogger("hermes_lark_streaming")
_CARD_CREATION_WAIT_SEC = 10.0


class StreamCardController(StreamingController):
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
        self._text_fallback_needed: set[str] = set()
        self._text_fallback_aliases: dict[str, set[str]] = {}

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

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        if self._loop is not None:
            return self._loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        return self._loop

    def _fire_and_forget(
        self, coro: Coroutine[Any, Any, Any], loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.Future[Any] | ConcurrentFuture:
        if loop is None:
            loop = self._get_loop()
        if loop is not None and loop.is_running():
            task = asyncio.ensure_future(coro, loop=loop)
            task.add_done_callback(self._on_bg_task_done)
            return task
        fut: ConcurrentFuture = ConcurrentFuture()
        threading.Thread(target=lambda: self._run_in_thread(coro, fut), daemon=True).start()
        return fut

    @staticmethod
    def _run_in_thread(coro: Coroutine[Any, Any, Any], fut: ConcurrentFuture) -> None:
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(coro)
            fut.set_result(result)
        except Exception as exc:
            fut.set_exception(exc)
        finally:
            new_loop.close()

    def on_message_started(
        self,
        *,
        message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        if not message_id or not chat_id:
            _logger.warning("on_message_started: missing message_id, chat=%s", chat_id[:12])
            return
        self._prune_stale_sessions()
        loop = self._get_loop()
        if loop is None:
            return
        self._ensure_init_sync()
        session = CardSession(message_id, chat_id, loop)
        if anchor_id and anchor_id != message_id:
            session.anchor_id = anchor_id
            self._sessions[anchor_id] = session
        self._sessions[message_id] = session
        _logger.info("session created: msg=%s chat=%s anchor=%s", message_id[:12], chat_id[:12], (anchor_id or "")[:12])
        session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

    def _ensure_init_sync(self) -> None:
        if self._initialized:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._fire_and_forget(self._ensure_init(), loop)
        else:
            import asyncio as _asyncio
            try:
                _asyncio.run(self._ensure_init())
            except Exception:
                _logger.debug("sync init failed", exc_info=True)

    def on_thinking(self, *, message_id: str, text: str) -> bool:
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_thinking"):
            return False
        if session.segment_state is None:
            return False
        session.segment_state.on_reasoning_delta(text)
        self._schedule_flush(session)
        return True

    def on_reasoning(self, *, message_id: str, text: str) -> bool:
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_reasoning"):
            return False
        if session.segment_state is None:
            return False
        session.segment_state.on_reasoning_delta(text)
        self._schedule_flush(session)
        return True

    def on_tool_update(
        self,
        *,
        message_id: str,
        tool_name: str = "",
        status: str = "started",
        detail: str = "",
    ) -> bool:
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_tool_update"):
            return False
        if session.tool_use is None:
            return False

        if status == "started":
            session.tool_use.record_start(tool_name)
        else:
            is_error = status in ("error", "failed")
            session.tool_use.record_end(
                tool_name,
                error=detail if is_error else "",
                output="" if is_error else detail,
            )

        session.segment_state.on_tool_event(len(session.tool_use.build_display_steps()))
        self._schedule_flush(session)
        return True

    def on_answer(self, *, message_id: str, text: str) -> bool:
        """答案文本增量（流式）."""
        if not self.enabled:
            return False
        session = self._get_active_session(message_id)
        if session is None or session.guard.should_skip("on_answer"):
            return False
        if session.segment_state is None:
            return False

        answer_text = strip_reasoning_tags(text)
        if not answer_text:
            return False

        session.segment_state.on_answer_delta(answer_text)
        self._schedule_flush(session)
        return True

    def on_aborted(self, *, message_id: str) -> None:
        """用户 /stop 导致消息被中断."""
        if not self.enabled:
            return
        session = self._get_active_session(message_id)
        if session is None:
            return

        session.state = SessionState.ABORTED
        session.flush.mark_completed()
        _logger.info("on_aborted: msg=%s state=ABORTED", message_id[:12])

        self._complete_session(session)

    def on_interrupted(
        self,
        *,
        old_message_id: str,
        new_message_id: str,
        chat_id: str,
        anchor_id: str | None = None,
    ) -> None:
        """用户发送新消息导致前一条消息被中断 — abort A + create B."""
        if not self.enabled:
            return

        old_session = self._get_active_session(old_message_id)
        if old_session is not None:
            old_session.state = SessionState.ABORTED
            old_session.flush.mark_completed()
            _logger.info(
                "on_interrupted: abort old msg=%s",
                old_message_id[:12],
            )
            self._complete_session(old_session)

        if new_message_id not in self._sessions:
            loop = self._get_loop()
            if loop is not None:
                reply_anchor_id = anchor_id if anchor_id and anchor_id != new_message_id else None
                session = CardSession(new_message_id, chat_id, loop)
                session.anchor_id = reply_anchor_id
                self._sessions[new_message_id] = session
                if reply_anchor_id:
                    self._sessions[reply_anchor_id] = session
                _logger.info(
                    "on_interrupted: create new msg=%s chat=%s anchor=%s",
                    new_message_id[:12],
                    chat_id[:12],
                    (reply_anchor_id or new_message_id)[:12],
                )
                session.create_task = self._fire_and_forget(self._do_create_card(session), loop)

        self._interrupt_map[old_message_id] = new_message_id
        for key, val in list(self._interrupt_map.items()):
            if val == old_message_id:
                self._interrupt_map[key] = new_message_id

    async def on_completed_wait(
        self,
        *,
        message_id: str,
        answer: str = "",
        duration: float = 0.0,
        model: str = "",
        tokens: dict | None = None,
        context: dict | None = None,
    ) -> bool:
        """消息处理完成，并等待卡片真正收尾后返回是否已发送."""
        if not self.enabled:
            return False
        session = self._completion_session(message_id)
        if session is None:
            return False
        message_id = session.message_id

        if not await self._wait_for_card_creation(session):
            _logger.info("on_completed_wait: msg=%s card creation not ready, yielding to gateway", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        if session.state == SessionState.FAILED:
            _logger.info("on_completed_wait: msg=%s state=FAILED, yielding to gateway", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        if not session.has_card:
            _logger.info("on_completed_wait: msg=%s has no card, yielding to gateway", message_id[:12])
            self._mark_text_fallback_needed(session)
            self._cleanup(message_id)
            return False

        _logger.info(
            "on_completed_wait: msg=%s has_card=%s state=%s",
            message_id[:12],
            session.has_card,
            session.state,
        )

        self._apply_completion_payload(
            session=session,
            answer=answer,
            duration=duration,
            model=model,
            tokens=tokens,
            context=context,
        )

        sent = await self._complete_session_wait(session)

        # ── interaction card: detect clarify/approval and send standalone card ──
        if sent and answer:
            self._fire_and_forget(
                self._maybe_send_interaction_card(session, answer),
                session._loop,
            )

        return sent

    def on_cron_deliver(
        self,
        *,
        chat_id: str,
        content: str,
        loop: asyncio.AbstractEventLoop,
        task_name: str = "",
        run_time: str = "",
    ) -> bool:
        """Cron 推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._do_cron_deliver(chat_id, content, task_name=task_name, run_time=run_time), loop
        )
        try:
            future.result(timeout=30)
            _logger.info("cron card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("cron card delivery failed", exc_info=True)
            return False

    async def on_background_deliver(
        self,
        *,
        chat_id: str,
        preview: str,
        content: str,
        reply_to_message_id: str | None = None,
    ) -> bool:
        """Background 任务完成推送 — 包装为静态卡片发送，成功返回 True."""
        if not self.enabled or not content or not chat_id:
            return False
        try:
            await self._do_background_deliver(
                chat_id,
                preview,
                content,
                reply_to_message_id=reply_to_message_id,
            )
            _logger.info("background card delivered: chat=%s len=%d", chat_id[:12], len(content))
            return True
        except Exception:
            _logger.warning("background card delivery failed", exc_info=True)
            return False

    def defer_background_review(
        self,
        *,
        message_id: str,
        text: str,
        sender: Callable[[str], Any],
    ) -> bool:
        """暂存 Hermes background review 通知，等卡片收尾后再发送."""
        if not self.enabled or not text or not callable(sender):
            return False
        session = self._get_active_session(message_id)
        if session is None:
            return False
        with session.deferred_background_review_lock:
            if session.deferred_background_review_closed:
                return False
            session.deferred_background_reviews.append((text, sender))
        return True

    def _flush_deferred_background_reviews(self, session: CardSession) -> None:
        lock = getattr(session, "deferred_background_review_lock", None)
        reviews = getattr(session, "deferred_background_reviews", None)
        if lock is None or reviews is None:
            return
        with lock:
            session.deferred_background_review_closed = True
            pending = list(reviews)
            reviews.clear()
        for text, sender in pending:
            try:
                sender(text)
            except Exception:
                _logger.debug("background review sender failed", exc_info=True)

    # ── interaction card support ──────────────────────────────────────

    async def _maybe_send_interaction_card(self, session: CardSession, answer: str) -> None:
        """Detect clarify/approval messages and send a standalone interaction card."""
        info = classify_message(answer)
        if info is None:
            return

        try:
            await self._ensure_init()
        except RuntimeError:
            return

        if self._client is None:
            return

        try:
            card = build_interaction_card(
                interaction_type=info["type"],
                question=info["question"],
                options=info["options"],
                is_danger=info.get("is_danger", False),
            )
            await self._client.send_card_to_chat(
                chat_id=session.chat_id,
                card=card,
            )
            _logger.info(
                "interaction card sent: msg=%s type=%s",
                session.message_id[:12],
                info["type"],
            )
        except Exception:
            _logger.debug("interaction card send failed", exc_info=True)

    # ── internal helpers ───────────────────────────────────────────────

    def _cleanup(self, message_id: str) -> None:
        session = self._sessions.pop(message_id, None)
        if session is None:
            return
        anchor = getattr(session, "anchor_id", None)
        if anchor and self._sessions.get(anchor) is session:
            del self._sessions[anchor]
        stale_keys = [k for k, v in self._interrupt_map.items() if v == message_id]
        for k in stale_keys:
            del self._interrupt_map[k]
        session.flush.mark_completed()
        if session.image_resolver:
            session.image_resolver.cancel_pending()

    def _completion_session(self, message_id: str) -> CardSession | None:
        session = self._sessions.get(message_id)
        if session is not None and (not session.state.is_terminal or session.state == SessionState.FAILED):
            return session

        redirected_id = self._interrupt_map.pop(message_id, None)
        if redirected_id is not None:
            _logger.info(
                "on_completed: redirect msg=%s -> msg=%s",
                message_id[:12],
                redirected_id[:12],
            )
            redirected = self._sessions.get(redirected_id)
            if redirected is not None and not redirected.state.is_terminal:
                return redirected
        return None

    async def _wait_for_card_creation(self, session: CardSession) -> bool:
        task = session.create_task
        if task is None:
            return True
        try:
            if isinstance(task, asyncio.Future):
                await asyncio.wait_for(task, timeout=_CARD_CREATION_WAIT_SEC)
            else:
                await asyncio.wait_for(asyncio.wrap_future(task), timeout=_CARD_CREATION_WAIT_SEC)
            return True
        except TimeoutError:
            _logger.warning(
                "card creation timed out: msg=%s timeout=%.1fs",
                session.message_id[:12],
                _CARD_CREATION_WAIT_SEC,
            )
            task.cancel()
            session.mark_failed()
            return False
        except asyncio.CancelledError:
            session.mark_failed()
            return False
        except Exception:
            _logger.debug("card creation task failed", exc_info=True)
            return False

    def _apply_completion_payload(
        self,
        *,
        session: CardSession,
        answer: str,
        duration: float,
        model: str,
        tokens: dict | None,
        context: dict | None,
    ) -> None:
        if answer and session.segment_state and not any(
            seg.type == SegmentType.ANSWER for seg in session.segment_state.segments
        ):
            final_answer = strip_reasoning_tags(answer)
            if final_answer:
                session.segment_state.on_answer_delta(final_answer)

        session.footer = {
            "duration": duration,
            "model": model,
            **( {"input_tokens": tokens.get("input_tokens")} if tokens else {}),
            **( {"output_tokens": tokens.get("output_tokens")} if tokens else {}),
            **( {"context_used": context.get("used_tokens")} if context else {}),
            **( {"context_max": context.get("max_tokens")} if context else {}),
        }

    def _complete_session(self, session: CardSession) -> None:
        """异步完成当前流式卡片."""
        session.flush.mark_completed()
        self._fire_and_forget(self._do_complete_card(session), session._loop)

    async def _complete_session_wait(self, session: CardSession) -> bool:
        """完成当前流式卡片，并等待最终 API 结果."""
        session.flush.mark_completed()
        return await self._do_complete_card(session)

    def _prune_stale_sessions(self) -> None:
        now = time.time()
        stale = [mid for mid, s in self._sessions.items() if mid is not None and now - s.created_at > self._session_ttl]
        for mid in stale:
            _logger.warning("pruning stale session: msg=%s", mid[:12])
            self._cleanup(mid)

    @staticmethod
    def _on_bg_task_done(fut: asyncio.Future[Any] | ConcurrentFuture) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            return
        except Exception:
            _logger.warning("background task failed", exc_info=True)

    # Forward to StreamingController base
    def _do_create_card(self, session: CardSession) -> Coroutine[Any, Any, None]:
        return StreamingController._do_create_card(self, session)

    def _do_complete_card(self, session: CardSession) -> Coroutine[Any, Any, bool]:
        return StreamingController._do_complete_card(self, session)

    def _do_cron_deliver(self, chat_id: str, content: str, **kw: Any) -> Coroutine[Any, Any, None]:
        return StreamingController._do_cron_deliver(self, chat_id, content, **kw)

    def _do_background_deliver(self, chat_id: str, preview: str, content: str, **kw: Any) -> Coroutine[Any, Any, None]:
        return StreamingController._do_background_deliver(self, chat_id, preview, content, **kw)

    def _get_active_session(self, message_id: str) -> CardSession | None:
        return StreamingController._get_active_session(self, message_id)

    def _schedule_flush(self, session: CardSession) -> None:
        return StreamingController._schedule_flush(self, session)

    def consume_text_fallback(self, message_id: str) -> bool:
        return StreamingController.consume_text_fallback(self, message_id)

    def _mark_text_fallback_needed(self, session: CardSession) -> None:
        return StreamingController._mark_text_fallback_needed(self, session)


_controller: StreamCardController | None = None
_controller_lock = threading.Lock()


def get_controller() -> StreamCardController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = StreamCardController()
        return _controller
