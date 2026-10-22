"""出站消息链发送（``event.send`` / ``send_by_session`` 共用）。

AstrBot 规范：遍历 ``MessageChain`` 的组件，用组件自带的
``convert_to_file_path()`` / ``get_file()`` 解析媒体，不自己猜 ``file``/``url``。
QWPad 每条文本上限约 2000 字符，超长自动分片。

组件映射：
  Plain   → /msg/sendText（含 At 时改用 /msg/sendHyperText）
  At      → hypertext 段 subtype=1（text 为 userId；AtAll 用 "0"）
  Reply   → 引用当前消息（由事件的 reply anchor 提供完整原消息结构）
  Image   → /cloud/cdnBigUpload(fileType=1) + /msg/sendImage
  Record  → ffmpeg→SILK + /cloud/cdnBigUpload(fileType=5) + /msg/sendVoice
  Video   → /cloud/cdnBigUpload(fileType=4) + /msg/sendVideo
  File    → /cloud/cdnBigUpload(fileType=5) + /msg/sendFile
  Share / Location → 退化成可读文本
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import subprocess
from typing import Any

from astrbot import logger
from astrbot.api.message_components import (
    At,
    AtAll,
    File,
    Image,
    Location,
    Plain,
    Record,
    Reply,
    Share,
    Video,
)

from .qwpad_client import (
    FILE_TYPE_FILE,
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    QWPadClient,
    QWPadError,
)
from .qwpad_media import encode_voice_to_silk, safe_filename

MAX_MESSAGE_LENGTH = 2000
# 同一会话的发送串行化，避免两条回复的分片互相穿插。
_SEND_LOCKS: dict[str, asyncio.Lock] = {}

_MARKDOWN_RULES = (
    (r"\*\*(.+?)\*\*", r"\1"),  # 粗体
    (r"__(.+?)__", r"\1"),
    (r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1"),  # 斜体
    (r"`([^`\n]+)`", r"\1"),  # 行内代码
    (r"^```\w*$", ""),  # 代码围栏行
    (r"!\[([^\]]*)\]\(([^)\s]+)\)", r"\2"),  # 图片 → url（先于链接）
    (r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)"),  # 链接 → 文本 (url)
    (r"^#{1,6}\s+", ""),  # 标题
    (r"^&gt;\s?", ""),
    (r"^>\s?", ""),  # 引用
)


def strip_markdown(text: str) -> str:
    """企业微信不渲染 markdown —— 转成可读纯文本。"""
    out = text or ""
    for pattern, repl in _MARKDOWN_RULES:
        out = re.sub(pattern, repl, out, flags=re.MULTILINE)
    return out


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """按字符数分片：先按换行，再硬切超长段落。"""
    limit = max(200, int(limit or MAX_MESSAGE_LENGTH))
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for paragraph in text.split("\n"):
        candidate = f"{buffer}\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return [c for c in (chunk.strip() for chunk in chunks) if c]


def _lock_for(client: QWPadClient, chat_id: str) -> asyncio.Lock:
    key = f"{id(client)}:{chat_id}"
    lock = _SEND_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SEND_LOCKS[key] = lock
    return lock


def _reply_payload(anchor: dict[str, Any] | None) -> dict[str, Any] | None:
    """只保留 openapi 认可的引用字段，避免把内部字段发给服务端。"""
    if not anchor:
        return None
    payload = {
        key: anchor.get(key)
        for key in (
            "msgUniqueIdentifier",
            "showName",
            "timeStamp",
            "type",
            "userId",
            "msgData",
        )
        if anchor.get(key) not in (None, "")
    }
    if not payload.get("msgUniqueIdentifier"):
        return None
    return payload


async def _send_text_piece(
    client: QWPadClient,
    chat_id: str,
    text: str,
    *,
    reply: dict[str, Any] | None,
    ats: list[dict[str, Any]],
) -> dict[str, Any]:
    """发送一段已分片文本（可带 @ 和引用）。"""
    if ats:
        # 有 @ 时必须走 hypertext：sendText 的纯文本 @ 不会真正提醒对方。
        segments: list[dict[str, Any]] = []
        if reply:
            # 引用气泡 + 可见正文：首段空串，避免服务端只生成「仅引用」气泡。
            segments.append({"subtype": 0, "text": ""})
        segments.extend(ats)
        segments.append({"subtype": 0, "text": text})
        return await client.send_hyper_text(chat_id, segments, reply=reply)
    if reply:
        # 实测（2026-10-22）：带 reply 的单段正文会被客户端隐藏，两段（空串+正文）可正常渲染。
        return await client.send_hyper_text(
            chat_id,
            [{"subtype": 0, "text": ""}, {"subtype": 0, "text": text}],
            reply=reply,
        )
    return await client.send_text(chat_id, text)


async def send_text(
    client: QWPadClient,
    chat_id: str,
    content: str,
    *,
    reply: dict[str, Any] | None = None,
    limit: int = MAX_MESSAGE_LENGTH,
    strip_md: bool = True,
) -> dict[str, Any]:
    """发送（自动分片的）纯文本；首片带引用。"""
    text = strip_markdown(content) if strip_md else (content or "")
    pieces = split_message(text, limit)
    if not pieces:
        return {"success": False, "error": "empty message", "sent": 0}
    last: dict[str, Any] = {}
    for index, piece in enumerate(pieces):
        last = await _send_text_piece(
            client,
            chat_id,
            piece,
            reply=reply if index == 0 else None,
            ats=[],
        )
    return {"success": True, "sent": len(pieces), "receipt": last}


async def _probe_video(path: str) -> tuple[int, int]:
    """返回 ``(duration_seconds, cover_size)``。

    QWPad 要求 duration 与上传结果配套；本地上传没有封面/时长元数据时，
    用 ffprobe 取时长、封面大小留 0（服务端允许）。
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0, 0
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        duration = int(math.ceil(float((out or b"").strip() or 0)))
        return max(0, duration), 0
    except Exception:  # noqa: BLE001 - 时长只是元数据
        return 0, 0


async def _send_media_component(
    client: QWPadClient,
    chat_id: str,
    component: Any,
    *,
    reply: dict[str, Any] | None,
) -> bool:
    """发送单个媒体组件；返回是否成功。"""
    if isinstance(component, Image):
        path = await component.convert_to_file_path()
        name = safe_filename(os.path.basename(path), "image.jpg")
        upload = await client.upload_file(path, FILE_TYPE_IMAGE)
        await client.send_image(chat_id, upload, name, reply=reply)
        return True

    if isinstance(component, Record):
        path = await _record_path(component)
        if not path:
            raise QWPadError("语音组件没有可读取的文件", retryable=False)
        encoded = await encode_voice_to_silk(path)
        if encoded is None:
            logger.warning("[QWPad] SILK 编码不可用，语音降级为文件: %s", path)
            upload = await client.upload_file(path, FILE_TYPE_FILE)
            await client.send_file(
                chat_id, upload, safe_filename(os.path.basename(path), "voice.amr"), reply=reply
            )
            return True
        silk, voice_time = encoded
        tmp = _temp_silk_path()
        try:
            with open(tmp, "wb") as fh:
                fh.write(silk)
            upload = await client.upload_file(tmp, FILE_TYPE_FILE)
            await client.send_voice(chat_id, upload, voice_time, reply=reply)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return True

    if isinstance(component, Video):
        path = await component.convert_to_file_path()
        duration, cover = await _probe_video(path)
        upload = await client.upload_file(path, FILE_TYPE_VIDEO)
        await client.send_video(
            chat_id,
            upload,
            safe_filename(os.path.basename(path), "video.mp4"),
            duration=duration,
            cover_size=cover or int(upload.get("fileThumbSize") or 0),
            reply=reply,
        )
        return True

    if isinstance(component, File):
        path = await component.get_file()
        if not path:
            raise QWPadError("文件组件没有可读取的文件", retryable=False)
        name = safe_filename(component.name or os.path.basename(path), "file")
        upload = await client.upload_file(path, FILE_TYPE_FILE)
        await client.send_file(chat_id, upload, name, reply=reply)
        return True

    return False


def _temp_silk_path() -> str:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

    directory = os.path.join(get_astrbot_temp_path(), "qwpad")
    os.makedirs(directory, exist_ok=True)
    import tempfile

    fd, path = tempfile.mkstemp(dir=directory, prefix="qwpad-voice-", suffix=".silk")
    os.close(fd)
    return path


async def _record_path(component: Record) -> str:
    """Record 的 file/url/path 因平台而异，统一解析成本地路径。"""
    for candidate in (component.path, component.file, component.url):
        if not candidate:
            continue
        if candidate.startswith("file://"):
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            path = url2pathname(unquote(urlparse(candidate).path))
            if os.path.isfile(path):
                return path
            continue
        if os.path.isfile(candidate):
            return candidate
    resolver = getattr(component, "_resolve_file_source", None)
    if resolver is not None:
        candidate = await resolver()
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def _at_segment(component: At) -> dict[str, Any] | None:
    if isinstance(component, AtAll) or str(component.qq) == "all":
        return {"subtype": 1, "text": "0"}
    qq = str(component.qq or "").strip()
    if not qq:
        return None
    return {"subtype": 1, "text": qq}


def _fallback_text(component: Any) -> str:
    if isinstance(component, Share):
        return "\n".join(p for p in (component.title, component.content, component.url) if p)
    if isinstance(component, Location):
        title = getattr(component, "title", "") or ""
        address = getattr(component, "address", "") or ""
        return f"[位置] {title} {address}".strip()
    return ""


async def send_chain(
    client: QWPadClient,
    chat_id: str,
    chain: list[Any],
    *,
    reply_anchor: dict[str, Any] | None = None,
    limit: int = MAX_MESSAGE_LENGTH,
    strip_md: bool = True,
) -> dict[str, Any]:
    """按消息链顺序发送；单个组件失败只记日志，不中断后续组件。"""
    async with _lock_for(client, chat_id):
        stats: dict[str, Any] = {"sent": 0, "failed": 0, "errors": [], "receipt": {}}
        pending_text: list[str] = []
        pending_ats: list[dict[str, Any]] = []
        reply_left = _reply_payload(reply_anchor)

        async def flush_text() -> None:
            nonlocal reply_left
            if not pending_text and not pending_ats:
                return
            text = "\n".join(pending_text).strip()
            ats = list(pending_ats)
            pending_text.clear()
            pending_ats.clear()
            text = strip_markdown(text) if strip_md else text
            pieces = split_message(text, limit)
            if not pieces:
                return
            for index, piece in enumerate(pieces):
                try:
                    receipt = await _send_text_piece(
                        client,
                        chat_id,
                        piece,
                        reply=reply_left if index == 0 else None,
                        ats=ats if index == 0 else [],
                    )
                    reply_left = None
                    stats["sent"] += 1
                    stats["receipt"] = receipt or {}
                except QWPadError as exc:
                    stats["failed"] += 1
                    stats["errors"].append(f"text: {exc}")
                    logger.warning("[QWPad] 发送文本失败: %s", exc)
                    # 引用失败时退化为不引用，避免整条回复丢失。
                    if reply_left is not None:
                        reply_left = None
                        try:
                            receipt = await _send_text_piece(
                                client,
                                chat_id,
                                piece,
                                reply=None,
                                ats=ats if index == 0 else [],
                            )
                            stats["sent"] += 1
                            stats["receipt"] = receipt or {}
                        except QWPadError as retry_exc:
                            logger.warning("[QWPad] 去引用重试仍失败: %s", retry_exc)

        for component in chain:
            if isinstance(component, Plain):
                pending_text.append(component.text or "")
                continue
            if isinstance(component, At):
                segment = _at_segment(component)
                if segment:
                    pending_ats.append(segment)
                continue
            if isinstance(component, Reply):
                # 事件已经按 message_id 放好 anchor；这里只记录引用意图，不重复发送。
                continue

            # 媒体/占位组件之前先把已积累的文本发出去，保证顺序。
            await flush_text()
            if isinstance(component, (Image, Record, Video, File)):
                try:
                    await _send_media_component(
                        client, chat_id, component, reply=reply_left
                    )
                    reply_left = None
                    stats["sent"] += 1
                except Exception as exc:  # noqa: BLE001 - 单个媒体失败不影响后续
                    stats["failed"] += 1
                    stats["errors"].append(f"{type(component).__name__}: {exc}")
                    logger.warning(
                        "[QWPad] 发送 %s 失败: %s", type(component).__name__, exc
                    )
                continue

            fallback = _fallback_text(component)
            if fallback:
                pending_text.append(fallback)

        await flush_text()
        stats["success"] = stats["failed"] == 0 and stats["sent"] > 0
        return stats


__all__ = [
    "MAX_MESSAGE_LENGTH",
    "send_chain",
    "send_text",
    "split_message",
    "strip_markdown",
]
