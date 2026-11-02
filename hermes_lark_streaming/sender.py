"""Gateway 离线时的正式投递入口，正文和附件保持同一发送契约."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list | None = None,
    force_document: bool = False,
) -> dict:
    from .adapter import FeishuStreamingAdapter, builtin

    if not await asyncio.to_thread(builtin._load_lark_oapi):
        return {"error": "Feishu SDK unavailable"}
    adapter = None
    try:
        adapter = FeishuStreamingAdapter(pconfig)
        # 独立发送器不启动入站连接；初始化同一个 SDK 出站客户端
        adapter._client = adapter._build_lark_client(builtin._sdk_domain(adapter._domain_name))
        metadata = {"thread_id": thread_id} if thread_id else None
        last = None
        if message.strip():
            last = await adapter.send(chat_id, message, metadata=metadata)
            if not last.success:
                return {"error": last.error or "Card delivery failed"}
        for path, _voice in media_files or []:
            if not Path(path).is_file():
                return {"error": "Media file not found"}
            suffix = Path(path).suffix.lower()
            send = adapter.send_document
            if not force_document:
                if suffix in builtin._MIGRATION_IMAGE_EXTS:
                    send = adapter.send_image_file
                elif suffix in builtin._MIGRATION_VIDEO_EXTS:
                    send = adapter.send_video
                elif suffix in builtin._MIGRATION_AUDIO_EXTS:
                    send = adapter.send_voice
            last = await send(chat_id, path, metadata=metadata)
            if not last.success:
                return {"error": last.error or "Media delivery failed"}
        return {"success": True, "message_id": last.message_id} if last else {"error": "Nothing to deliver"}
    except Exception:
        return {"error": "Feishu card/media delivery failed"}
    finally:
        if adapter is not None:
            await adapter.disconnect()
