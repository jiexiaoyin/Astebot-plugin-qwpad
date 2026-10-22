"""媒体：入站下载 / SILK 语音解码，出站任意音频 → SILK。

入站文件落到 AstrBot 临时目录，由调用方 ``event.track_temporary_local_file()``
交给事件结束时清理；出站只产生临时文件并立即删除。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from astrbot import logger

from .qwpad_client import QWPadClient, QWPadError
from .qwpad_messages import MediaRef

MAX_MEDIA_BYTES = 50 * 1024 * 1024
# 企业微信语音气泡上限（wpp 实测 60 KB），超限依次降码率。
MAX_SILK_BYTES = 60_000
VOICE_SAMPLE_RATE = 24_000
FFMPEG_TIMEOUT_S = 30.0
SILK_TIMEOUT_S = 30.0

_IMAGE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def _temp_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

    directory = Path(get_astrbot_temp_path()) / "qwpad"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _new_temp_path(suffix: str) -> str:
    fd, path = tempfile.mkstemp(
        dir=str(_temp_dir()), prefix="qwpad-", suffix=suffix or ".bin"
    )
    os.close(fd)
    return path


def safe_filename(name: str, fallback: str = "file") -> str:
    """聊天记录里的文件名可能带路径分隔符/控制字符，落盘前清洗。"""
    cleaned = _UNSAFE_NAME.sub("_", os.path.basename(name or "")).strip(" .")
    return cleaned[:120] or fallback


def sniff_image_suffix(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"GIF8"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def sniff_video_suffix(raw: bytes) -> str:
    if raw[4:8] == b"ftyp":
        return ".mp4"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    return ".mp4"


async def download_ref(client: QWPadClient, ref: MediaRef) -> bytes | None:
    """按媒体凭证下载原始字节。个人微信直链优先走 ``/cloud/wxDownload``。"""
    try:
        if ref.wx_candidates:
            return await client.download_wx_file(ref.as_dict())
        return await client.download_wxwork_file(ref.as_dict())
    except QWPadError as exc:
        logger.warning(
            "[QWPad] 下载%s失败 fileId=%s: %s", ref.kind, ref.file_id or "-", exc
        )
        return None


async def decode_silk_bytes(silk: bytes) -> str | None:
    """SILK → WAV 临时文件路径；pilk 不可用/解码失败返回 ``None``。"""
    if not silk:
        return None
    silk_path = _new_temp_path(".silk")
    wav_path = os.path.splitext(silk_path)[0] + ".wav"
    keep_wav = False
    try:
        Path(silk_path).write_bytes(silk)
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, _silk_to_wav_sync, silk_path, wav_path),
            timeout=SILK_TIMEOUT_S,
        )
        if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
            keep_wav = True
            return wav_path
        return None
    except Exception as exc:  # noqa: BLE001 - 解码失败由调用方降级成文件
        logger.warning("[QWPad] SILK 解码失败: %s", exc)
        return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(silk_path)
        if not keep_wav:
            with contextlib.suppress(OSError):
                os.unlink(wav_path)


def _silk_to_wav_sync(silk_path: str, wav_path: str) -> None:
    import pilk

    pilk.silk_to_wav(silk_path, wav_path, rate=VOICE_SAMPLE_RATE)


async def cache_media(client: QWPadClient, ref: MediaRef) -> tuple[str, str] | None:
    """下载一条媒体并落成 AstrBot 可用的本地文件。

    返回 ``(local_path, kind)``，kind ∈ image/voice/video/file；失败 ``None``。
    语音解码失败时按普通文件返回（至少模型能拿到原始 SILK 附件说明）。
    """
    raw = await download_ref(client, ref)
    if not raw:
        return None
    if len(raw) > MAX_MEDIA_BYTES:
        logger.warning(
            "[QWPad] %s 超过大小上限 %s 字节，丢弃", ref.kind, len(raw)
        )
        return None

    try:
        if ref.kind == "image":
            path = _new_temp_path(sniff_image_suffix(raw))
            Path(path).write_bytes(raw)
            return path, "image"

        if ref.kind == "video":
            path = _new_temp_path(sniff_video_suffix(raw))
            Path(path).write_bytes(raw)
            return path, "video"

        if ref.kind == "voice":
            wav = await decode_silk_bytes(raw)
            if wav:
                return wav, "voice"
            # 非 SILK（AMR/MP3/微信语音直链等）按文件交出去。
            suffix = ".amr" if raw.startswith(b"#!AMR") else ".silk"
            path = _new_temp_path(suffix)
            Path(path).write_bytes(raw)
            return path, "file"

        name = safe_filename(ref.filename or "file")
        path = _new_temp_path(Path(name).suffix or ".bin")
        Path(path).write_bytes(raw)
        return path, "file"
    except OSError as exc:
        logger.warning("[QWPad] 缓存%s失败: %s", ref.kind, exc)
        return None


# ── 出站语音 ────────────────────────────────────────────────────────────────
async def encode_voice_to_silk(audio_path: str) -> tuple[bytes, int] | None:
    """任意音频 → ``(silk_bytes, voice_time_seconds)``；失败返回 ``None``。

    链路：ffmpeg 24kHz mono s16le → pilk（tencent=True）。超过 60KB 时依次
    降低编码码率；仍超限则放弃，由调用方降级成文件发送。
    """
    if not audio_path or not os.path.isfile(audio_path):
        return None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("[QWPad] 未安装 ffmpeg，语音降级为文件")
        return None

    pcm_path = _new_temp_path(".pcm")
    silk_path = _new_temp_path(".silk")
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            audio_path,
            "-ar",
            str(VOICE_SAMPLE_RATE),
            "-ac",
            "1",
            "-f",
            "s16le",
            pcm_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(
            proc.communicate(), timeout=FFMPEG_TIMEOUT_S
        )
        if proc.returncode != 0 or not os.path.isfile(pcm_path):
            logger.warning("[QWPad] ffmpeg 转 PCM 失败: %s", (err or b"")[:200])
            return None
        pcm_size = os.path.getsize(pcm_path)
        if pcm_size <= 0:
            return None

        loop = asyncio.get_running_loop()
        for max_rate in (24000, 16000, 12000, 8000):
            silk_rate = max_rate
            with contextlib.suppress(OSError):
                os.unlink(silk_path)
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        _encode_pcm_sync,
                        pcm_path,
                        silk_path,
                        max_rate,
                        silk_rate,
                    ),
                    timeout=SILK_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[QWPad] SILK 编码失败 (max_rate=%s): %s", max_rate, exc)
                return None
            silk = Path(silk_path).read_bytes()
            if silk.startswith(b"\x02") and len(silk) <= MAX_SILK_BYTES:
                voice_time = max(1, math.ceil(pcm_size / 48 / 1000))
                return silk, voice_time
            logger.info(
                "[QWPad] SILK %s 字节超限 (max_rate=%s)，降码率重试",
                len(silk),
                max_rate,
            )
        return None
    finally:
        for path in (pcm_path, silk_path):
            with contextlib.suppress(OSError):
                os.unlink(path)


def _encode_pcm_sync(
    pcm_path: str, silk_path: str, max_rate: int, silk_rate: int | None
) -> None:
    from pilk import SilkEncoder

    encoder = SilkEncoder(pcm_rate=VOICE_SAMPLE_RATE, silk_rate=silk_rate, max_rate=max_rate)
    encoder.encode(pcm_path, silk_path, tencent=True)


__all__ = [
    "MAX_MEDIA_BYTES",
    "cache_media",
    "decode_silk_bytes",
    "download_ref",
    "encode_voice_to_silk",
    "safe_filename",
    "sniff_image_suffix",
]
