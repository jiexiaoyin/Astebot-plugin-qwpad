"""游标与去重状态：按平台实例落盘，重启不重放旧消息。

状态文件位于 ``data/plugin_data/astrbot_plugin_qwpad/<platform_id>/``，
不写插件源码目录（插件升级/重装不会丢状态）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from astrbot import logger

DEDUPE_CAPACITY = 2000
PLUGIN_NAME = "astrbot_plugin_qwpad"


def plugin_data_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME


def safe_instance_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "qwpad")).strip("._")
    return cleaned[:80] or "qwpad"


def instance_dir(platform_id: str) -> Path:
    return plugin_data_dir() / safe_instance_id(platform_id)


def token_fingerprint(token: str) -> str:
    """TOKEN 的短指纹——只用于日志/文件，绝不明文落盘。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class CursorStore:
    """``{lastEventId, tokenFingerprint, updatedAt}`` 的小状态文件，原子写。

    游标按平台实例 id 落盘；如果实例换了 TOKEN（不同账号的事件 id 空间
    不通用），旧游标必须失效，否则会跳过新账号的消息或越界拉取。
    """

    def __init__(self, directory: Path, fingerprint: str = ""):
        self.path = directory / "cursor.json"
        self.fingerprint = fingerprint or ""
        self._last = 0

    def load(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            logger.warning("[QWPad] 读取游标失败 %s: %s", self.path, exc)
            return 0
        try:
            self._last = int(raw.get("lastEventId") or 0)
        except (TypeError, ValueError, AttributeError):
            self._last = 0
        stored = str(raw.get("tokenFingerprint") or "")
        if self.fingerprint and stored and stored != self.fingerprint:
            logger.warning(
                "[QWPad] 平台实例 %s 的 TOKEN 已更换（%s → %s），游标重置",
                self.path.parent.name,
                stored,
                self.fingerprint,
            )
            self.reset()
            return 0
        return self._last

    @property
    def last(self) -> int:
        return self._last

    def save(self, event_id: int) -> None:
        event_id = int(event_id or 0)
        if event_id <= 0 or event_id == self._last:
            return
        try:
            atomic_write_json(
                self.path,
                {
                    "lastEventId": event_id,
                    "tokenFingerprint": self.fingerprint,
                    "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except OSError as exc:
            # 只有写成功才推进内存游标：写失败时下一轮还会重试落盘。
            logger.warning("[QWPad] 写游标失败 %s: %s", self.path, exc)
            return
        self._last = event_id

    def reset(self) -> None:
        """清空游标（插件页「从头拉取」用）。"""
        self._last = 0
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[QWPad] 删除游标失败 %s: %s", self.path, exc)


class DedupeSet:
    """有界的「已处理消息 id」集合（LRU 淘汰）。"""

    def __init__(self, capacity: int = DEDUPE_CAPACITY):
        self._items: OrderedDict[str, None] = OrderedDict()
        self.capacity = capacity

    def __contains__(self, key: Any) -> bool:
        return key in self._items

    def add(self, key: Any) -> None:
        if not key:
            return
        key = str(key)
        self._items[key] = None
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def seed(self, keys: Iterable[Any]) -> None:
        for key in keys:
            self.add(key)

    def __len__(self) -> int:
        return len(self._items)


__all__ = [
    "DEDUPE_CAPACITY",
    "PLUGIN_NAME",
    "CursorStore",
    "DedupeSet",
    "atomic_write_json",
    "instance_dir",
    "plugin_data_dir",
    "safe_instance_id",
    "token_fingerprint",
]
