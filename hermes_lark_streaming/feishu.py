"""飞书 Open API 客户端 — 基于 lark-oapi SDK."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    BatchUpdateCardRequest,
    BatchUpdateCardRequestBody,
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

_logger = logging.getLogger("hermes_lark_streaming")


def _sanitize_message(msg: str) -> str:
    """从错误消息中移除 token 和 secret."""
    msg = re.sub(r'(tenant_access_token["\s:=]+)([A-Za-z0-9_-]{10,})', r"\1***", msg)
    msg = re.sub(r'(app_secret["\s:=]+)([A-Za-z0-9]{10,})', r"\1***", msg)
    msg = re.sub(r"(Bearer\s+)([A-Za-z0-9_-]{10,})", r"\1***", msg)
    return msg


class FeishuAPIError(RuntimeError):
    """飞书 API 错误，携带 API 错误码."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code

    def extract_sub_code(self) -> int | None:
        """从 msg 字符串中提取子错误码.

        格式: "Failed to create card content, ext=ErrCode: 11310; ..."
        """
        m = re.search(r"ErrCode:\s*(\d+)", str(self))
        if m:
            return int(m.group(1))
        return None


CARDKIT_RATE_LIMITED = 230020  # 频控
CARDKIT_CONTENT_FAILED = 230099  # 卡片内容创建失败（通用码，需检查子错误）
CARDKIT_ELEMENT_LIMIT = 11310  # 子码: 卡片元素数量超限
CARDKIT_STREAMING_CLOSED = 300309  # 卡片流式模式已关闭
MSG_NOT_FOUND = 1000023  # 消息不存在/已删除


@dataclass(frozen=True)
class FeishuClientConfig:
    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-apis"

    def __post_init__(self) -> None:
        if not isinstance(self.app_id, str) or not self.app_id.strip():
            raise ValueError("app_id is required")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip():
            raise ValueError("app_secret is required")


class FeishuClient:
    """飞书 REST API 封装 — 基于 lark-oapi SDK.

    SDK 自动管理 tenant_access_token 的获取和刷新.
    """

    def __init__(self, config: FeishuClientConfig) -> None:
        self.config = config
        builder = lark.Client.builder().app_id(config.app_id).app_secret(config.app_secret)
        self._client = builder.build()

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        """检查 SDK 响应，失败时抛出 FeishuAPIError."""
        if not response.success():
            code = response.code or 0
            msg = response.msg or ""
            raise FeishuAPIError(
                _sanitize_message(f"{operation}: code={code}, msg={msg}"),
                code,
            )

    @staticmethod
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> str:
        """回复消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(ReplyMessageRequestBody.builder().msg_type("interactive").content(self._dumps(card)).build())
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        self._check(resp, "reply_card")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_card: response missing message_id")

    async def reply_card_by_id(self, message_id: str, card_id: str) -> str:
        """通过 card_id 回复 CardKit 卡片消息，返回 message_id."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.areply(request)
        self._check(resp, "reply_card_by_id")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("reply_card_by_id: response missing message_id")

    async def send_card_by_id(self, chat_id: str, card_id: str) -> str:
        """发送 CardKit 卡片到 chat（非回复），返回 message_id."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(self._dumps({"type": "card", "data": {"card_id": card_id}}))
                .build()
            )
            .build()
        )
        resp = await self._client.im.v1.message.acreate(request)
        self._check(resp, "send_card_by_id")
        if resp.data and resp.data.message_id:
            return str(resp.data.message_id)
        raise FeishuAPIError("send_card_by_id: response missing message_id")

    async def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        """PATCH 更新已发送的卡片（IM 降级通道）."""
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(self._dumps(card)).build())
            .build()
        )
        resp = await self._client.im.v1.message.apatch(request)
        self._check(resp, "update_card")

    async def cardkit_create(self, card: dict[str, Any]) -> str:
        """创建 CardKit 实体，返回 card_id."""
        request = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(self._dumps(card)).build())
            .build()
        )
        resp = await self._client.cardkit.v1.card.acreate(request)
        self._check(resp, "cardkit_create")
        if resp.data and resp.data.card_id:
            return str(resp.data.card_id)
        raise FeishuAPIError("cardkit_create: response missing card_id")

    async def cardkit_stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int = 0,
    ) -> None:
        """流式更新卡片内指定 element 的内容（打字机效果）."""
        body_builder = ContentCardElementRequestBody.builder().content(content)
        body_builder = body_builder.sequence(sequence)
        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(body_builder.build())
            .build()
        )
        resp = await asyncio.to_thread(
            self._client.cardkit.v1.card_element.content,
            request,
        )
        self._check(resp, "cardkit_stream_element")

    async def cardkit_update(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 0,
    ) -> None:
        """全量更新 CardKit 卡片."""
        body_builder = UpdateCardRequestBody.builder().card(
            Card.builder().type("card_json").data(self._dumps(card)).build()
        )
        body_builder = body_builder.sequence(sequence)
        request = UpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        resp = await self._client.cardkit.v1.card.aupdate(request)
        self._check(resp, "cardkit_update")

    async def cardkit_batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int = 0,
    ) -> None:
        """局部更新 CardKit 卡片（增删改组件）."""
        body_builder = BatchUpdateCardRequestBody.builder().sequence(sequence).actions(self._dumps(actions))
        request = BatchUpdateCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        resp = await self._client.cardkit.v1.card.abatch_update(request)
        self._check(resp, "cardkit_batch_update")

    async def cardkit_close_streaming(self, card_id: str, sequence: int = 0) -> None:
        """关闭 CardKit 卡片的流式模式."""
        body_builder = SettingsCardRequestBody.builder().settings(self._dumps({"streaming_mode": False}))
        body_builder = body_builder.sequence(sequence)
        request = SettingsCardRequest.builder().card_id(card_id).request_body(body_builder.build()).build()
        resp = await self._client.cardkit.v1.card.asettings(request)
        self._check(resp, "cardkit_close_streaming")

    async def upload_image(self, image_url: str) -> str | None:
        """下载远程图片并上传到飞书，返回 img_key."""
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(
                None,
                self._download_image,
                image_url,
            )
        except Exception:
            _logger.debug("image upload failed for %s", image_url, exc_info=True)
            return None

        if data is None:
            return None

        file = io.BytesIO(data)
        request = (
            CreateImageRequest.builder()
            .request_body(CreateImageRequestBody.builder().image_type("message").image(file).build())
            .build()
        )
        resp = await self._client.im.v1.image.acreate(request)
        if resp.success() and resp.data and resp.data.image_key:
            return str(resp.data.image_key)
        return None

    @staticmethod
    def _download_image(url: str, timeout: int = 15) -> bytes | None:
        """同步下载图片（在线程池中运行）."""
        try:
            req = Request(url, headers={"User-Agent": "hermes-lark-streaming/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                return bytes(resp.read())
        except (URLError, OSError):
            _logger.debug("image download failed: %s", url)
            return None
