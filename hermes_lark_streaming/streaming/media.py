"""``MEDIA:`` 附件解析与补投 — 补上流式卡片路径漏掉的附件.

Hermes 网关只从 ``final_response`` 扫描 ``MEDIA:<path>`` 指令再投递文件
(``gateway/run_turn.py``：``if already_sent and not failed: if response and adapter:
await self._deliver_media_from_response(...)``)。流式卡片路径上有两处会让网关拿不到这段文本：

* 队列 follow-up 收尾：本插件把 ``result["final_response"] = ""``，网关随后无从扫描；
* 模型只走了流式 delta、``final_response`` 为空时（与插件无关，但同样丢附件）。

两种情况下卡片正文正常（来自 delta），附件却被静默丢弃。本模块从流式文本里重新解析
``MEDIA:`` 指令并直接调用飞书 API 补投；已经由网关自己投递的标签不重复发送。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..feishu import FeishuClient

_logger = logging.getLogger("hermes_lark_streaming")

# 单轮最多补投的文件数，防止一条回复刷屏。
MAX_MEDIA_FILES_PER_TURN = 10
# 飞书 im/v1/files 流式文件上限 30MB。
MAX_MEDIA_FILE_BYTES = 30 * 1024 * 1024

# 与 Hermes ``gateway/platforms/base.py`` 的 MEDIA_DELIVERY_EXTS 同形状：只投递已知类型的文件，
# 避免把正文里随手提到的路径当成附件。
DELIVERABLE_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp",
        ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac",
        ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".markdown", ".epub",
        ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
        ".pptx", ".ppt", ".odp", ".key",
        ".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar",
        ".html", ".htm", ".py", ".sh", ".log",
    }
)

# 图片按 image 消息发送（聊天内联显示）；与 Hermes `gateway/platforms/base.py::_IMAGE_EXTS` 同口径。
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

# 飞书上传时的 file_type（决定预览方式）；未列出的一律 stream。
FEISHU_FILE_TYPES = {
    ".mp4": "mp4", ".mov": "mp4", ".avi": "mp4", ".m4v": "mp4",
    ".ogg": "opus", ".opus": "opus",
    ".pdf": "pdf",
    ".doc": "doc", ".docx": "doc",
    ".xls": "xls", ".xlsx": "xls",
    ".ppt": "ppt", ".pptx": "ppt",
}

# 中文句读 / 全角标点：，。！？；：、（）、【】《》「」『』（按码位构造，避免 RUF001 歧义告警）
_CJK_TERMINATORS = "".join(
    chr(cp)
    for cp in (
        0xFF0C, 0x3002, 0xFF01, 0xFF1F, 0xFF1B, 0xFF1A, 0x3001, 0xFF08, 0xFF09,
        0x3010, 0x3011, 0x300A, 0x300B, 0x300C, 0x300D, 0x300E, 0x300F,
    )
)
_DELIMITERS = ")]}>,;:"

# fenced code block 内的路径是示例，不是附件。
_FENCED_CODE_RE = re.compile(r"^(?: {0,3})(?:```|~~~).*?^(?: {0,3})(?:```|~~~)[ \t]*$", re.MULTILINE | re.DOTALL)

# ``MEDIA:`` 指令：路径允许 ``~/``、``/``、``X:\`` / ``X:/`` 开头，可为反引号/引号包裹；
# 只认行首或空白/左括号之后的标签，避免正文里的示例被当作附件（Hermes 同为显式指令契约）；
# 裸路径在空白、引号或中文句读处结束。
_MEDIA_TAG_RE = re.compile(
    r"""(?:^|[\s(])MEDIA:\s*(?:"""
    r"""(?P<quoted>`[^`\n]+?`|"[^"\n]+?"|'[^'\n]+?')"""
    r"""|"""
    r"""(?P<bare>(?:~/|/|[A-Za-z]:[/\\])[^\s<>"'`""" + _CJK_TERMINATORS + r"""]+)"""
    r""")""",
    re.IGNORECASE | re.MULTILINE,
)


def _mask_fenced_code(text: str) -> str:
    """把 fenced code block 换成等量空白（保留偏移量，便于按位置删除）."""
    return _FENCED_CODE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def normalize_media_path(raw: str) -> str | None:
    """把标签里的原始路径规范成绝对路径；空路径返回 None."""
    path = raw.strip().strip("`").strip("\"'").strip()
    path = path.rstrip(_CJK_TERMINATORS + _DELIMITERS).rstrip(".")
    if not path:
        return None
    try:
        return os.path.expanduser(path)
    except (OSError, ValueError):
        return None


def _has_deliverable_extension(path: str) -> bool:
    return Path(path).suffix.lower() in DELIVERABLE_EXTS


def _is_image_path(path: str) -> bool:
    """图片走 image 消息内联显示，其余走 file 消息."""
    return Path(path).suffix.lower() in IMAGE_EXTS


def is_deliverable_media_file(path: str) -> bool:
    """路径存在、是普通文件、扩展名在白名单内、体积在飞书上限内."""
    try:
        if not _has_deliverable_extension(path):
            return False
        stat = os.stat(path)
    except (OSError, ValueError):
        return False
    return os.path.isfile(path) and 0 < stat.st_size <= MAX_MEDIA_FILE_BYTES


def extract_media_paths(text: str) -> list[str]:
    """按出现顺序返回文本里可投递的本地文件路径（去重、校验存在性）."""
    if not text or "MEDIA:" not in text.upper():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for match in _MEDIA_TAG_RE.finditer(_mask_fenced_code(text)):
        raw = match.group("quoted") or match.group("bare") or ""
        path = normalize_media_path(raw)
        if path is None or path in seen or not is_deliverable_media_file(path):
            if path is not None:
                _logger.debug("skipping undeliverable MEDIA path: %s", path)
            continue
        seen.add(path)
        paths.append(path)
    return paths


def hook_media_paths(media_files: object) -> list[str]:
    """归一注入点传来的 ``media_files``，返回可投递的本地路径列表.

    Hermes 在调用 cron / background 注入钩子**之前**就用 ``extract_media`` 把 ``MEDIA:`` 标签从正文里
    剥掉了（``cron/scheduler_delivery.py``），路径只留在 ``media_files`` 里，形如
    ``[(path, is_voice), ...]``。钩子把该变量原样传进来，这里归一成路径（也接受裸路径列表），
    并复用本模块的存在性/体积/类型校验。
    """
    if not media_files or isinstance(media_files, (str, bytes)):
        return []
    if not isinstance(media_files, (list, tuple, set, frozenset)):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for entry in media_files:
        raw = entry[0] if isinstance(entry, (tuple, list)) and entry else entry
        if not isinstance(raw, (str, os.PathLike)):
            continue
        path = os.path.expanduser(str(raw))
        if path in seen:
            continue
        seen.add(path)
        if is_deliverable_media_file(path):
            paths.append(path)
        else:
            _logger.warning("hook media: undeliverable path skipped: %s", path)
    return paths[:MAX_MEDIA_FILES_PER_TURN]


def media_paths_to_deliver(
    *,
    streamed: str,
    gateway_text: str = "",
    gateway_delivers: bool = True,
) -> list[str]:
    """决定插件要补投哪些路径.

    ``gateway_delivers`` 为 True（网关会拿 ``gateway_text`` 自己投递 MEDIA 附件）时，
    只补 ``streamed`` 里网关看不到的路径，避免重复投递；为 False（调用方随后会清空
    ``final_response``，或本轮已失败）时，两段文本里的路径全部由插件投递。
    """
    streamed_paths = extract_media_paths(streamed)
    if not gateway_delivers:
        known = extract_media_paths(gateway_text)
        return _dedupe([*known, *streamed_paths])
    handled = set(extract_media_paths(gateway_text))
    return [path for path in streamed_paths if path not in handled]


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out[:MAX_MEDIA_FILES_PER_TURN]


def _directive_spans(text: str) -> list[tuple[int, int]]:
    """返回 ``MEDIA:<path>`` 指令的字符区间（跳过 fenced code block，扩展名须在白名单内）."""
    spans: list[tuple[int, int]] = []
    for match in _MEDIA_TAG_RE.finditer(_mask_fenced_code(text)):
        raw = match.group("quoted") or match.group("bare") or ""
        path = normalize_media_path(raw)
        if path is None or not _has_deliverable_extension(path):
            continue
        start = match.start() if match.group(0)[: len("MEDIA:")].upper() == "MEDIA:" else match.start() + 1
        spans.append((start, match.end()))
    return spans


def strip_media_directives(text: str) -> str:
    """从卡片正文里去掉 ``MEDIA:`` 指令（附件由本模块或网关投递，正文不该出现路径）.

    与 Hermes ``strip_media_directives_for_display`` 同口径：已知可投递扩展名的标签一律去掉；
    其它（扩展名未知、示例代码块内）保持可见，以便用户看清未被投递的路径。
    """
    if not text or "MEDIA:" not in text.upper():
        return text
    spans = _directive_spans(text)
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return re.sub(r"\n{3,}", "\n\n", "".join(pieces))


async def deliver_media_files(
    client: FeishuClient,
    chat_id: str,
    paths: list[str],
    *,
    reply_to_message_id: str | None = None,
) -> int:
    """逐个上传并发送文件消息，返回成功条数.

    单条失败只记日志、继续下一条；本函数不抛异常（附件投递失败不能影响卡片收尾）。
    """
    if not chat_id or not paths:
        return 0
    sent = 0
    for path in paths[:MAX_MEDIA_FILES_PER_TURN]:
        try:
            image_key = await client.upload_local_image(path) if _is_image_path(path) else None
            if image_key:
                # 图片走 image 消息：聊天里直接内联显示，而不是一个文件卡片。
                await client.send_image_to_chat(
                    chat_id,
                    image_key,
                    reply_to_message_id=reply_to_message_id,
                )
            else:
                file_type = FEISHU_FILE_TYPES.get(Path(path).suffix.lower(), "stream")
                file_key = await client.upload_file(path, file_type=file_type)
                if not file_key:
                    _logger.warning("media delivery: upload failed for %s", path)
                    continue
                await client.send_file_to_chat(
                    chat_id,
                    file_key,
                    reply_to_message_id=reply_to_message_id,
                )
            sent += 1
        except Exception:
            _logger.warning("media delivery failed for %s", path, exc_info=True)
    return sent
