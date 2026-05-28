"""streaming.image 测试 — ImageResolver 缓存、上传、失败、pending 逻辑."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_lark_streaming.streaming.image import ImageResolver


class TestCancelPending:
    def test_cancel_pending_clears_tasks(self) -> None:
        r, _ = _make_resolver()
        fake_task = MagicMock()
        r._pending["https://a.com/1.png"] = fake_task
        r._pending["https://b.com/2.png"] = MagicMock()
        r.cancel_pending()
        fake_task.cancel.assert_called_once()
        assert len(r._pending) == 0

    def test_cancel_pending_empty_is_safe(self) -> None:
        r, _ = _make_resolver()
        r.cancel_pending()  # 不应抛异常
        assert len(r._pending) == 0


def _make_resolver(
    upload_result: str | None = "img_v3_fake123",
    on_resolved: object = None,
    timeout: float = 5.0,
) -> tuple[ImageResolver, MagicMock]:
    """返回 (resolver, mock_client) 用于检查."""
    client = MagicMock()
    client.upload_image = AsyncMock(return_value=upload_result)
    resolver = ImageResolver(
        client=client,
        timeout=timeout,
        on_image_resolved=on_resolved,
    )
    return resolver, client


class TestResolveImages:
    def test_no_images_unchanged(self) -> None:
        r, _ = _make_resolver()
        assert r.resolve_images("plain text") == "plain text"

    def test_img_key_preserved(self) -> None:
        r, _ = _make_resolver()
        result = r.resolve_images("![alt](img_v3_existing)")
        assert "img_v3_existing" in result

    def test_cache_hit_replaces_url(self) -> None:
        r, _ = _make_resolver()
        r._cache["https://example.com/img.png"] = "img_v3_cached"
        result = r.resolve_images("![alt](https://example.com/img.png)")
        assert "img_v3_cached" in result
        assert "example.com" not in result

    def test_failed_url_stripped(self) -> None:
        r, _ = _make_resolver()
        r._failed.add("https://example.com/bad.png")
        result = r.resolve_images("![alt](https://example.com/bad.png)")
        assert "bad.png" not in result
        assert result.strip() == ""

    def test_pending_url_stripped(self) -> None:
        r, _ = _make_resolver()
        fake_task = MagicMock()
        r._pending["https://example.com/uploading.png"] = fake_task
        result = r.resolve_images("![alt](https://example.com/uploading.png)")
        assert "uploading.png" not in result

    def test_multiple_images(self) -> None:
        r, _ = _make_resolver()
        r._cache["https://a.com/img.png"] = "img_v3_a"
        text = "![a](https://a.com/img.png) and ![b](https://b.com/new.png)"
        result = r.resolve_images(text)
        assert "img_v3_a" in result
        assert "b.com" not in result

    @pytest.mark.asyncio
    async def test_new_url_stripped_and_upload_started(self) -> None:
        r, _ = _make_resolver()
        result = r.resolve_images("![alt](https://example.com/new.png)")
        assert "new.png" not in result
        assert "https://example.com/new.png" in r._pending
        await asyncio.sleep(0.05)
        assert "https://example.com/new.png" not in r._pending


class TestUpload:
    @pytest.mark.asyncio
    async def test_successful_upload_caches(self) -> None:
        r, _ = _make_resolver(upload_result="img_v3_new")
        r.resolve_images("![alt](https://example.com/upload.png)")
        await asyncio.sleep(0.05)
        assert "https://example.com/upload.png" in r._cache
        assert r._cache["https://example.com/upload.png"] == "img_v3_new"

    @pytest.mark.asyncio
    async def test_successful_upload_removes_pending(self) -> None:
        r, _ = _make_resolver(upload_result="img_v3_new")
        r.resolve_images("![alt](https://example.com/upload.png)")
        assert "https://example.com/upload.png" in r._pending
        await asyncio.sleep(0.05)
        assert "https://example.com/upload.png" not in r._pending

    @pytest.mark.asyncio
    async def test_failed_upload_adds_to_failed(self) -> None:
        r, _ = _make_resolver(upload_result=None)
        r.resolve_images("![alt](https://example.com/fail.png)")
        await asyncio.sleep(0.05)
        assert "https://example.com/fail.png" in r._failed

    @pytest.mark.asyncio
    async def test_upload_exception_adds_to_failed(self) -> None:
        client = MagicMock()
        client.upload_image = AsyncMock(side_effect=RuntimeError("network error"))
        r = ImageResolver(client=client)
        r.resolve_images("![alt](https://example.com/error.png)")
        await asyncio.sleep(0.05)
        assert "https://example.com/error.png" in r._failed

    @pytest.mark.asyncio
    async def test_failed_url_not_retried(self) -> None:
        r, client = _make_resolver(upload_result=None)
        r.resolve_images("![alt](https://example.com/fail.png)")
        await asyncio.sleep(0.05)
        client.upload_image.reset_mock()
        r.resolve_images("![alt](https://example.com/fail.png)")
        client.upload_image.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_image_resolved_callback(self) -> None:
        callback = MagicMock()
        r, _ = _make_resolver(upload_result="img_v3_new", on_resolved=callback)
        r.resolve_images("![alt](https://example.com/cb.png)")
        await asyncio.sleep(0.05)
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_callback_on_failure(self) -> None:
        callback = MagicMock()
        r, _ = _make_resolver(upload_result=None, on_resolved=callback)
        r.resolve_images("![alt](https://example.com/fail.png)")
        await asyncio.sleep(0.05)
        callback.assert_not_called()


class TestResolveAwait:
    @pytest.mark.asyncio
    async def test_waits_for_uploads_then_replaces(self) -> None:
        r, _ = _make_resolver(upload_result="img_v3_done")
        text = "![alt](https://example.com/wait.png)"
        result = await r.resolve_await(text)
        assert "img_v3_done" in result
        assert "example.com" not in result

    @pytest.mark.asyncio
    async def test_returns_early_when_no_images(self) -> None:
        r, _ = _make_resolver()
        result = await r.resolve_await("no images here")
        assert result == "no images here"

    @pytest.mark.asyncio
    async def test_timeout_returns_partial(self) -> None:
        client = MagicMock()

        async def slow_upload(url: str) -> str | None:
            await asyncio.sleep(10)
            return "img_v3_slow"

        client.upload_image = slow_upload
        r = ImageResolver(client=client, timeout=0.1)
        text = "![alt](https://example.com/slow.png)"
        result = await r.resolve_await(text)
        assert "example.com" not in result

    @pytest.mark.asyncio
    async def test_already_cached_returned_immediately(self) -> None:
        r, client = _make_resolver()
        r._cache["https://example.com/cached.png"] = "img_v3_cached"
        text = "![alt](https://example.com/cached.png)"
        result = await r.resolve_await(text)
        assert "img_v3_cached" in result
        client.upload_image.assert_not_awaited()
