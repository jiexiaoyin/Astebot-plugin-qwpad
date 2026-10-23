"""异步文件 I/O 小工具。

AstrBot 的事件循环承载所有平台/插件，媒体文件又可能达到几十 MiB；在协程里
直接 ``open``/``write_bytes``/``unlink`` 会阻塞事件循环。这里统一丢到工作线程。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path


async def write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    await asyncio.to_thread(Path(path).write_bytes, data)


async def read_bytes(path: str | os.PathLike[str]) -> bytes:
    return await asyncio.to_thread(Path(path).read_bytes)


async def unlink_quietly(path: str | os.PathLike[str]) -> None:
    def _unlink() -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)

    await asyncio.to_thread(_unlink)


async def file_size(path: str | os.PathLike[str]) -> int:
    return await asyncio.to_thread(os.path.getsize, path)


__all__ = ["file_size", "read_bytes", "unlink_quietly", "write_bytes"]
