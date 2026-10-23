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
import math
import os
import re
import shutil
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
    BIG_FILE_TYPE_FILE,
    BIG_FILE_TYPE_VIDEO,
    FILE_TYPE_FILE,
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    MAX_CDN_UPLOAD_BYTES,
    QWPadClient,
    QWPadError,
)
from .qwpad_io import unlink_quietly, write_bytes
from .qwpad_media import encode_voice_to_silk, safe_filename

MAX_MESSAGE_LENGTH = 2000
# 同一会话的发送串行化，避免两条回复的分片互相穿插。
_SEND_LOCKS: dict[str, asyncio.Lock] = {}
# 发送锁按 (client, chat) 累积；会话很多时只清理「当前没人持有」的锁，
# 避免无限增长（正在用的锁被移除会导致同一会话并发发送）。
MAX_SEND_LOCKS = 512

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
        if len(_SEND_LOCKS) > MAX_SEND_LOCKS:
            for old_key, old_lock in list(_SEND_LOCKS.items()):
                if old_lock is lock or old_lock.locked():
                    continue
                _SEND_LOCKS.pop(old_key, None)
                if len(_SEND_LOCKS) <= MAX_SEND_LOCKS // 2:
                    break
    return lock


async def _with_retry(call: Any, *, attempts: int = 3) -> Any:
    """对可重试的 QWPad 错误做有限重试（网络抖动/超时）。

    注意：超时可能发生在服务端已经接收之后，极端情况下会重复发送一条；
    相比静默丢回复，这里选择「至少一次」。
    """
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except QWPadError as exc:
            if not exc.retryable or attempt >= attempts:
                raise
            logger.warning(
                "[QWPad] 发送失败（第 %s/%s 次，%.1fs 后重试）: %s",
                attempt,
                attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            delay *= 2


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
        duration = math.ceil(float((out or b"").strip() or 0))
        return max(0, duration), 0
    except Exception:  # noqa: BLE001 - 时长只是元数据
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        return 0, 0


def _needs_big_upload(path: str) -> bool:
    """普通 CDN 要求 <20MiB；达到/超过时改走 BigCDN。"""
    try:
        return os.path.getsize(path) >= MAX_CDN_UPLOAD_BYTES
    except OSError:
        return False


async def _send_file_path(
    client: QWPadClient,
    chat_id: str,
    path: str,
    name: str,
    *,
    reply: dict[str, Any] | None,
) -> None:
    if _needs_big_upload(path):
        upload = await client.upload_big_file(path, BIG_FILE_TYPE_FILE)
        await client.send_big_file(chat_id, upload, name, reply=reply)
        return
    upload = await client.upload_file(path, FILE_TYPE_FILE)
    await client.send_file(chat_id, upload, name, reply=reply)


async def _send_video_path(
    client: QWPadClient,
    chat_id: str,
    path: str,
    name: str,
    *,
    reply: dict[str, Any] | None,
) -> None:
    if _needs_big_upload(path):
        upload = await client.upload_big_file(path, BIG_FILE_TYPE_VIDEO)
        await client.send_big_video(chat_id, upload, name, reply=reply)
        return
    duration, cover = await _probe_video(path)
    upload = await client.upload_file(path, FILE_TYPE_VIDEO)
    await client.send_video(
        chat_id,
        upload,
        name,
        duration=duration,
        cover_size=cover,
        reply=reply,
    )


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
            await _send_file_path(
                client,
                chat_id,
                path,
                safe_filename(os.path.basename(path), "voice.amr"),
                reply=reply,
            )
            return True
        silk, voice_time = encoded
        tmp = await asyncio.to_thread(_temp_silk_path)
        try:
            await write_bytes(tmp, silk)
            upload = await client.upload_file(tmp, FILE_TYPE_FILE)
            await client.send_voice(chat_id, upload, voice_time, reply=reply)
        finally:
            await unlink_quietly(tmp)
        return True

    if isinstance(component, Video):
        path = await component.convert_to_file_path()
        await _send_video_path(
            client,
            chat_id,
            path,
            safe_filename(os.path.basename(path), "video.mp4"),
            reply=reply,
        )
        return True

    if isinstance(component, File):
        path = await component.get_file()
        if not path:
            raise QWPadError("文件组件没有可读取的文件", retryable=False)
        name = safe_filename(component.name or os.path.basename(path), "file")
        await _send_file_path(client, chat_id, path, name, reply=reply)
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
    """Record 的 file/url/path 因平台而异，统一解析成本地路径。

    AstrBot 的 TTS（``use_file_service=true``）会把语音做成
    ``Record(file="<callback>/api/file/<token>", url=同值)``。这类来源必须走
    组件的 ``convert_to_file_path()`` 下载/转码，不能只判断本地文件是否存在。
    """
    for candidate in (component.path, component.file, component.url):
        if not candidate:
            continue
        if candidate.startswith(("http://", "https://", "base64://", "data:")):
            break  # 交给 convert_to_file_path()
        if candidate.startswith("file://"):
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            path = url2pathname(unquote(urlparse(candidate).path))
            if os.path.isfile(path):
                return path
            continue
        if os.path.isfile(candidate):
            return candidate
    try:
        converted = await component.convert_to_file_path()
    except Exception as exc:  # noqa: BLE001 - 来源不可用由调用方降级
        logger.warning("[QWPad] 语音来源解析失败: %s", exc)
        return ""
    if converted and os.path.isfile(converted):
        return converted
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
        return "\n".join(
            p for p in (component.title, component.content, component.url) if p
        )
    if isinstance(component, Location):
        # 核心 Location 组件的字段是 title/content/lat/lon（没有 address）。
        title = getattr(component, "title", "") or ""
        content = getattr(component, "content", "") or ""
        lat = getattr(component, "lat", None)
        lon = getattr(component, "lon", None)
        coords = f"{lat},{lon}" if lat is not None and lon is not None else ""
        return f"[位置] {title} {content} {coords}".strip()
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
                # 只有 @（例如 AtAll）没有正文时也要把提醒发出去，不能静默丢弃。
                if ats:
                    try:
                        receipt = await _with_retry(
                            lambda: _send_text_piece(
                                client, chat_id, "", reply=reply_left, ats=ats
                            )
                        )
                        reply_left = None
                        stats["sent"] += 1
                        stats["receipt"] = receipt or {}
                    except QWPadError as exc:
                        stats["failed"] += 1
                        stats["errors"].append(f"at: {exc}")
                        logger.warning("[QWPad] 发送 @ 提醒失败: %s", exc)
                return
            for index, piece in enumerate(pieces):
                try:
                    receipt = await _with_retry(
                        # 立即 await, 不存在循环变量晚绑定问题
                        lambda: _send_text_piece(
                            client,
                            chat_id,
                            piece,  # noqa: B023
                            reply=reply_left if index == 0 else None,  # noqa: B023
                            ats=ats if index == 0 else [],  # noqa: B023
                        )
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
                            # 去引用重试成功：第一条失败已计入 failed，回滚掉。
                            stats["failed"] = max(0, stats["failed"] - 1)
                            if stats["errors"]:
                                stats["errors"].pop()
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
                    await _with_retry(
                        # 立即 await, 不存在循环变量晚绑定问题
                        lambda: _send_media_component(
                            client, chat_id, component, reply=reply_left  # noqa: B023
                        )
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
