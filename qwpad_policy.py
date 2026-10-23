"""准入策略：决定一条已经解析出来的聊天消息是否进入 AstrBot。

准入 ≠ 唤醒。这里只回答「要不要交给 AstrBot 事件流」；群里是否触发 AI 回复
由 AstrBot 核心的唤醒阶段（At / 唤醒词）决定。``group_reply=all`` 只表示
「未 @ 的群消息也放行」，不会伪造 At 去强制回复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GROUP_REPLY_MODES = ("atbot", "all", "none")


def split_csv(value: Any) -> set[str]:
    """配置值（list 或逗号/空白分隔串）→ 去重集合。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = str(value or "").replace("\n", ",").replace(" ", ",").split(",")
    return {item.strip() for item in items if item and item.strip()}


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PolicySnapshot:
    """一次准入判断所需的全部策略（启动/配置重载时生成）。"""

    allow_users: frozenset[str] = frozenset()
    allow_groups: frozenset[str] = frozenset()
    block_users: frozenset[str] = frozenset()
    block_groups: frozenset[str] = frozenset()
    allow_all_users: bool = False
    group_reply: str = "atbot"

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> PolicySnapshot:
        mode = str(config.get("group_reply", "atbot") or "atbot").strip().lower()
        if mode not in GROUP_REPLY_MODES:
            mode = "atbot"
        return cls(
            allow_users=frozenset(split_csv(config.get("allow_users", ""))),
            allow_groups=frozenset(split_csv(config.get("allow_groups", ""))),
            block_users=frozenset(split_csv(config.get("block_users", ""))),
            block_groups=frozenset(split_csv(config.get("block_groups", ""))),
            allow_all_users=as_bool(config.get("allow_all_users"), False),
            group_reply=mode,
        )

    @property
    def permits_anyone(self) -> bool:
        return bool(self.allow_all_users or self.allow_users or self.allow_groups)


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def classify(policy: PolicySnapshot, inbox: Any) -> Admission:
    """按白/黑名单与群策略判断消息是否进入 AstrBot。"""
    user_id = str(getattr(inbox, "user_id", "") or "")
    chat_id = str(getattr(inbox, "chat_id", "") or "")
    is_group = getattr(inbox, "chat_type", "") == "group"

    if user_id and user_id in policy.block_users:
        return Admission(False, "blocked_user")
    if is_group and chat_id in policy.block_groups:
        return Admission(False, "blocked_group")

    if is_group:
        if policy.group_reply == "none":
            return Admission(False, "group_ignored")
        if (
            not policy.allow_all_users
            and chat_id not in policy.allow_groups
            and user_id not in policy.allow_users
        ):
            return Admission(False, "group_not_allowed")
        if policy.group_reply == "atbot" and not getattr(inbox, "mentions_me", False):
            return Admission(False, "group_not_mentioned")
        return Admission(True, "group")

    # 私聊
    if policy.allow_all_users or user_id in policy.allow_users:
        return Admission(True, "dm")
    return Admission(False, "user_not_allowed")


__all__ = [
    "GROUP_REPLY_MODES",
    "Admission",
    "PolicySnapshot",
    "as_bool",
    "as_int",
    "classify",
    "split_csv",
]
