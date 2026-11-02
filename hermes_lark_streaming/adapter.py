"""继承内置连接与权限，仅替换出站卡片展示."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import OrderedDict
from typing import Any

from gateway.config import StreamingConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session_context import get_session_env
from plugins.platforms.feishu import adapter as builtin

from .cardkit.builder import build_static_card
from .config import DEFAULT_DOMAIN, LARK_DOMAIN, Config, hermes_home
from .controller import StreamCardController
from .feishu import FeishuClient, FeishuClientConfig

_logger = logging.getLogger(__name__)
ACTIVE: weakref.WeakSet[Any] = weakref.WeakSet()


class FeishuStreamingAdapter(builtin.FeishuAdapter):
    SUPPORTS_NATIVE_STREAMING = True
    SUPPORTS_MESSAGE_EDITING = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.cards = StreamCardController(
            Config(hermes_home(), extra=config.extra),
            FeishuClient(
                FeishuClientConfig(
                    self._app_id,
                    self._app_secret,
                    LARK_DOMAIN if self._domain_name == "lark" else DEFAULT_DOMAIN,
                )
            ),
        )
        self._stream_cursor = StreamingConfig.from_dict(self.cards._cfg._reload().get("streaming", {})).cursor
        self._card_routes: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._card_loop: asyncio.AbstractEventLoop | None = None
        self._static_cards: OrderedDict[str, None] = OrderedDict()
        ACTIVE.add(self)

    async def handle_message(self, event: MessageEvent) -> None:
        self._card_loop = asyncio.get_running_loop()
        # 引用目标和入站消息身份分开保存，不通过 chat_id 猜测所属用户
        raw = event.raw_message
        body = getattr(getattr(raw, "event", None), "message", None)
        if isinstance(raw, dict):
            body = (raw.get("event") or {}).get("message", raw.get("message", raw))
        real_thread = body.get("thread_id") if isinstance(body, dict) else getattr(body, "thread_id", None)
        if event.reply_to_message_id and event.source.thread_id and body is not None and not real_thread:
            event.source.thread_id = None
        source_id = str(event.message_id or "")
        if source_id:
            self._card_routes[(event.source.chat_id, source_id)] = event.reply_to_message_id or source_id
            while len(self._card_routes) > 1024:
                self._card_routes.popitem(last=False)
        await super().handle_message(event)

    def supports_native_streaming(self, chat_type: str | None = None, metadata: dict | None = None) -> bool:
        return bool(self.cards._cfg.enabled)

    async def send_stream_frame(
        self,
        text: str,
        *,
        finalize: bool = False,
        chat_id: str | None = None,
        reply_to: str | None = None,
        turn_id: str = "",
        **kwargs: Any,
    ) -> bool:
        if not chat_id or not turn_id:
            return False
        self._card_loop = asyncio.get_running_loop()
        # 宿主中间帧带展示光标，比较累计正文前必须剥离；终态正文原样保留
        if not finalize and self._stream_cursor and text.endswith(self._stream_cursor):
            text = text[:-len(self._stream_cursor)]
        source_id = get_session_env("HERMES_SESSION_MESSAGE_ID") or reply_to or turn_id
        anchor = self._card_routes.get((chat_id, source_id), reply_to)
        try:
            return await self.cards.frame(
                turn_id,
                chat_id,
                text,
                reply_to=anchor,
                source_id=source_id,
                finalize=finalize,
                session_key=get_session_env("HERMES_SESSION_KEY"),
            )
        except Exception:
            _logger.exception("Native card frame failed; returning delivery failure to Hermes")
            return False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        md = metadata or {}
        if md.get("_interim_send") and not md.get("non_conversational"):
            source_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
            if source_id:
                message_id = await self.cards.interim(source_id, content)
                if message_id:
                    return SendResult(success=True, message_id=message_id)
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        # 后台通知按明确目标独立投递，不合并进前台活跃卡片
        if not content.strip():
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)
        anchor = reply_to or md.get("reply_to_message_id") or md.get("thread_id")
        try:
            card = build_static_card(content)
            assert self.cards._client is not None
            message_id = await self.cards._client.send_card_to_chat(
                chat_id,
                card,
                reply_to_message_id=anchor,
            )
            self._static_cards[message_id] = None
            while len(self._static_cards) > 512:
                self._static_cards.popitem(last=False)
            return SendResult(success=True, message_id=message_id)
        except Exception:
            _logger.warning("Static card failed; using built-in reply", exc_info=True)
            return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        if message_id not in self._static_cards:
            return await super().edit_message(chat_id, message_id, content, finalize=finalize)
        try:
            assert self.cards._client is not None
            await self.cards._client.update_card_message(message_id, build_static_card(content))
            return SendResult(success=True, message_id=message_id)
        except Exception:
            _logger.warning("Static card update failed", exc_info=True)
            return SendResult(success=False, error="Card update failed")

    async def send_or_update_status(
        self,
        chat_id: str,
        status_key: str,
        content: str,
        metadata: dict | None = None,
    ) -> SendResult:
        # 状态通报保留明确投递目标，不更新无法可靠关联的卡片
        return await super().send(chat_id, content, metadata=metadata)

    async def interrupt_session_activity(
        self,
        session_key: str,
        chat_id: str,
        metadata: dict | None = None,
    ) -> None:
        sources = {
            turn.source_id
            for turn in self.cards.turns.values()
            if turn.session.session_key == session_key and turn.session.chat_id == chat_id
        }
        for source_id in sources:
            await self.cards.close_source(source_id)

    async def disconnect(self) -> None:
        try:
            await self.cards.close()
        finally:
            ACTIVE.discard(self)
            await super().disconnect()
