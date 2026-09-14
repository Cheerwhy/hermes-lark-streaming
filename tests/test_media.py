"""streaming.media 测试 — MEDIA: 指令解析、投递决策、正文清洗与附件投递."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_lark_streaming.streaming.media import (
    DELIVERABLE_EXTS,
    MAX_MEDIA_FILES_PER_TURN,
    deliver_media_files,
    extract_media_paths,
    hook_media_paths,
    is_deliverable_media_file,
    media_paths_to_deliver,
    strip_media_directives,
)


def _make_file(tmp_path, name: str = "report.md", content: str = "hello") -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


class TestIsDeliverableMediaFile:
    def test_accepts_existing_known_extension(self, tmp_path) -> None:
        assert is_deliverable_media_file(_make_file(tmp_path)) is True

    def test_rejects_missing_file(self, tmp_path) -> None:
        assert is_deliverable_media_file(str(tmp_path / "nope.md")) is False

    def test_rejects_unknown_extension(self, tmp_path) -> None:
        assert is_deliverable_media_file(_make_file(tmp_path, "data.weird")) is False

    def test_rejects_empty_file(self, tmp_path) -> None:
        assert is_deliverable_media_file(_make_file(tmp_path, "empty.md", "")) is False

    def test_rejects_directory(self, tmp_path) -> None:
        assert is_deliverable_media_file(str(tmp_path)) is False

    def test_rejects_oversized_file(self, tmp_path, monkeypatch) -> None:
        path = _make_file(tmp_path)
        monkeypatch.setattr("hermes_lark_streaming.streaming.media.MAX_MEDIA_FILE_BYTES", 2)
        assert is_deliverable_media_file(path) is False


class TestExtractMediaPaths:
    def test_plain_tag(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert extract_media_paths(f"草稿在这里：\nMEDIA:{path}\n") == [path]  # noqa: RUF001

    def test_multiple_tags_in_order(self, tmp_path) -> None:
        a = _make_file(tmp_path, "a.md")
        b = _make_file(tmp_path, "b.pdf")
        text = f"MEDIA:{a}\n说明\nMEDIA:{b}\n"
        assert extract_media_paths(text) == [a, b]

    def test_dedupes_repeated_tag(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        text = f"MEDIA:{path}\nMEDIA:{path}\n"
        assert extract_media_paths(text) == [path]

    def test_quoted_path_with_spaces(self, tmp_path) -> None:
        path = _make_file(tmp_path, "with space.md")
        assert extract_media_paths(f'MEDIA:"{path}"') == [path]

    def test_backticked_path(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert extract_media_paths(f"MEDIA:`{path}`") == [path]

    def test_cjk_sentence_punctuation_is_not_part_of_path(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert extract_media_paths(f"MEDIA:{path}，其余见正文") == [path]  # noqa: RUF001

    def test_missing_file_is_ignored(self, tmp_path) -> None:
        assert extract_media_paths(f"MEDIA:{tmp_path / 'missing.md'}") == []

    def test_unknown_extension_is_ignored(self, tmp_path) -> None:
        path = _make_file(tmp_path, "blob.weird")
        assert extract_media_paths(f"MEDIA:{path}") == []

    def test_fenced_code_block_is_ignored(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        text = f"示例：\n```\nMEDIA:{path}\n```\n"  # noqa: RUF001
        assert extract_media_paths(text) == []

    def test_glued_to_word_is_not_a_directive(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert extract_media_paths(f"xMEDIA:{path}") == []

    def test_prose_url_is_not_a_directive(self, tmp_path) -> None:
        assert extract_media_paths("see https://example.com/MEDIA:/etc/passwd.md") == []

    def test_empty_and_none_like_inputs(self) -> None:
        assert extract_media_paths("") == []
        assert extract_media_paths("no directives here") == []

    def test_deliverable_extensions_include_common_docs(self) -> None:
        assert {".md", ".pdf", ".png", ".zip"} <= DELIVERABLE_EXTS


class TestHookMediaPaths:
    """注入钩子透传的 media_files 归一（Hermes 在调用钩子前已把 MEDIA 标签从正文剥走）."""

    def test_accepts_path_voice_tuples(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert hook_media_paths([(path, False)]) == [path]

    def test_voice_flagged_entry_is_still_delivered(self, tmp_path) -> None:
        path = _make_file(tmp_path, "note.mp3")
        assert hook_media_paths([(path, True)]) == [path]

    def test_accepts_bare_path_list(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert hook_media_paths([path]) == [path]

    def test_dedupes_and_keeps_order(self, tmp_path) -> None:
        a = _make_file(tmp_path, "a.md")
        b = _make_file(tmp_path, "b.png")
        assert hook_media_paths([(a, False), (b, False), (a, False)]) == [a, b]

    def test_drops_missing_and_unknown_and_empty_entries(self, tmp_path) -> None:
        keep = _make_file(tmp_path)
        assert (
            hook_media_paths(
                [
                    (str(tmp_path / "gone.png"), False),
                    (_make_file(tmp_path, "x.weird"), False),
                    (),
                    (None, False),
                    ("", False),
                    (keep, False),
                ]
            )
            == [keep]
        )

    @pytest.mark.parametrize("value", [None, "", "report.png", {"path": "x"}, 42])
    def test_ignores_non_list_like_inputs(self, value) -> None:
        assert hook_media_paths(value) == []

    def test_caps_at_per_turn_limit(self, tmp_path) -> None:
        entries = [
            (_make_file(tmp_path, f"f{i}.md"), False) for i in range(MAX_MEDIA_FILES_PER_TURN + 3)
        ]
        assert len(hook_media_paths(entries)) == MAX_MEDIA_FILES_PER_TURN


class TestMediaPathsToDeliver:
    def test_gateway_text_media_is_left_to_gateway(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert media_paths_to_deliver(
            streamed=f"MEDIA:{path}",
            gateway_text=f"MEDIA:{path}",
            gateway_delivers=True,
        ) == []

    def test_streamed_only_media_is_delivered(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert media_paths_to_deliver(
            streamed=f"MEDIA:{path}",
            gateway_text="",
            gateway_delivers=True,
        ) == [path]

    def test_gateway_delivers_false_delivers_union(self, tmp_path) -> None:
        a = _make_file(tmp_path, "a.md")
        b = _make_file(tmp_path, "b.md")
        assert media_paths_to_deliver(
            streamed=f"MEDIA:{b}",
            gateway_text=f"MEDIA:{a}",
            gateway_delivers=False,
        ) == [a, b]

    def test_gateway_delivers_false_dedupes_across_both(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        assert media_paths_to_deliver(
            streamed=f"MEDIA:{path}",
            gateway_text=f"MEDIA:{path}",
            gateway_delivers=False,
        ) == [path]

    def test_nothing_to_deliver(self) -> None:
        assert media_paths_to_deliver(streamed="", gateway_text="") == []


class TestStripMediaDirectives:
    def test_removes_directive_line(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        out = strip_media_directives(f"草稿写好了。\nMEDIA:{path}\n明天见")
        assert path not in out
        assert "草稿写好了。" in out
        assert "明天见" in out

    def test_keeps_inline_mention_without_path(self) -> None:
        text = "用 MEDIA: 语法可以附加文件。"
        assert strip_media_directives(text) == text

    def test_unknown_extension_left_visible(self, tmp_path) -> None:
        path = _make_file(tmp_path, "blob.weird")
        text = f"MEDIA:{path}"
        assert strip_media_directives(text) == text

    def test_collapses_blank_lines(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        out = strip_media_directives(f"a\n\nMEDIA:{path}\n\nb")
        assert "\n\n\n" not in out

    def test_plain_text_untouched(self) -> None:
        assert strip_media_directives("普通回复") == "普通回复"


class TestDeliverMediaFiles:
    @pytest.mark.asyncio
    async def test_uploads_and_sends_each_file(self, tmp_path) -> None:
        a = _make_file(tmp_path, "a.md")
        b = _make_file(tmp_path, "b.pdf")
        client = MagicMock()
        client.upload_file = AsyncMock(side_effect=["file_key_a", "file_key_b"])
        client.send_file_to_chat = AsyncMock(return_value="msg_id")

        sent = await deliver_media_files(client, "chat_1", [a, b])

        assert sent == 2
        assert client.upload_file.await_count == 2
        assert client.send_file_to_chat.await_count == 2
        first_call = client.send_file_to_chat.await_args_list[0]
        assert first_call.args == ("chat_1", "file_key_a")

    @pytest.mark.asyncio
    async def test_image_goes_out_as_inline_image(self, tmp_path) -> None:
        """图片走 image 消息（聊天内联显示），不是 file 消息."""
        chart = _make_file(tmp_path, "curve.png")
        client = MagicMock()
        client.upload_local_image = AsyncMock(return_value="img_key")
        client.send_image_to_chat = AsyncMock(return_value="msg_id")
        client.upload_file = AsyncMock()
        client.send_file_to_chat = AsyncMock()

        sent = await deliver_media_files(client, "chat_1", [chart], reply_to_message_id="card_1")

        assert sent == 1
        client.upload_local_image.assert_awaited_once_with(chart)
        assert client.send_image_to_chat.await_args.args == ("chat_1", "img_key")
        assert client.send_image_to_chat.await_args.kwargs["reply_to_message_id"] == "card_1"
        client.upload_file.assert_not_awaited()
        client.send_file_to_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_image_upload_failure_falls_through_to_file(self, tmp_path) -> None:
        """image 通道失败时不能让附件静默丢失——退回 file 消息."""
        chart = _make_file(tmp_path, "curve.png")
        client = MagicMock()
        client.upload_local_image = AsyncMock(return_value=None)
        client.upload_file = AsyncMock(return_value="file_key")
        client.send_file_to_chat = AsyncMock(return_value="msg_id")
        client.send_image_to_chat = AsyncMock()

        sent = await deliver_media_files(client, "chat_1", [chart])

        assert sent == 1
        client.upload_file.assert_awaited_once_with(chart, file_type="stream")
        client.send_image_to_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_type_follows_extension(self, tmp_path) -> None:
        pdf = _make_file(tmp_path, "doc.pdf")
        client = MagicMock()
        client.upload_file = AsyncMock(return_value="file_key")
        client.send_file_to_chat = AsyncMock(return_value="msg_id")

        await deliver_media_files(client, "chat_1", [pdf])

        assert client.upload_file.await_args.kwargs["file_type"] == "pdf"

    @pytest.mark.asyncio
    async def test_upload_failure_skips_send_and_continues(self, tmp_path) -> None:
        a = _make_file(tmp_path, "a.md")
        b = _make_file(tmp_path, "b.md")
        client = MagicMock()
        client.upload_file = AsyncMock(side_effect=[None, "file_key_b"])
        client.send_file_to_chat = AsyncMock(return_value="msg_id")

        sent = await deliver_media_files(client, "chat_1", [a, b])

        assert sent == 1
        assert client.send_file_to_chat.await_count == 1

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self, tmp_path) -> None:
        path = _make_file(tmp_path)
        client = MagicMock()
        client.upload_file = AsyncMock(side_effect=RuntimeError("boom"))
        client.send_file_to_chat = AsyncMock()

        assert await deliver_media_files(client, "chat_1", [path]) == 0
        client.send_file_to_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_inputs_do_nothing(self) -> None:
        client = MagicMock()
        client.upload_file = AsyncMock()
        assert await deliver_media_files(client, "", ["/tmp/x.md"]) == 0
        assert await deliver_media_files(client, "chat_1", []) == 0
        client.upload_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_is_capped(self, tmp_path) -> None:
        paths = [_make_file(tmp_path, f"f{i}.md") for i in range(MAX_MEDIA_FILES_PER_TURN + 3)]
        client = MagicMock()
        client.upload_file = AsyncMock(return_value="file_key")
        client.send_file_to_chat = AsyncMock(return_value="msg_id")

        sent = await deliver_media_files(client, "chat_1", paths)

        assert sent == MAX_MEDIA_FILES_PER_TURN
