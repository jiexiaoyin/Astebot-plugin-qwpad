"""事件解析：把 ``/events``（unified）条目映射成适配器内部结构。

纯数据变换，不发网络请求、不依赖 AstrBot —— 便于单测。
真实 unified 条目形状（2026-10 openapi + live /events）::

    {"id": 127, "createdAt": "2026-10-22 00:07:35", "type": 15000,
     "display": {"category": "chat", "text": "...", "senderId": "...", "roomId": "0",
                 "senderName": "...", "messageId": "...", "messageType": {...},
                 "content": {"kind": "text", "text": "...", "media": {...}}},
     "message": {"msgType": 0, "msgData": {...}, "rawBodyBase64": null,
                 "structured": {"senderId": "...", "fromRoomId": 0, "receiverId": "...",
                                "msgType": 0, "msgUniqueIdentifier": "...", "msgData": {...},
                                "isRevoked": false, "timestamp": 1792627702}}}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot import logger

from .qwpad_client import (
    FILE_TYPE_FILE,
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    MSG_APP_TEXT_CARD,
    MSG_CHANNEL,
    MSG_CONTENT_FILE,
    MSG_CONTENT_FILE_CDN,
    MSG_CONTENT_FILE_WWFTN,
    MSG_CONTENT_FILE_WWFTN_ENCRYPT,
    MSG_CONTENT_IMAGE,
    MSG_CONTENT_IMAGE_CDN,
    MSG_CONTENT_IMAGE_P2P,
    MSG_CONTENT_IMAGE_WWFTN,
    MSG_CONTENT_IMAGE_WWFTN_ENCRYPT,
    MSG_CONTENT_VOICE_WWFTN,
    MSG_CONTENT_VOICE_WWFTN_ENCRYPT,
    MSG_CONTENT_VIDEO_WWFTN,
    MSG_CONTENT_VIDEO_WWFTN_ENCRYPT,
    MSG_EMOTION,
    MSG_FILE,
    MSG_FORWARD,
    MSG_IMAGE,
    MSG_INNER_ROOM_NOTICE,
    MSG_INVITE_RECORD,
    MSG_LINK,
    MSG_LOCATION,
    MSG_MEMBER_OPERATION,
    MSG_MINI_PROGRAM,
    MSG_MIXED,
    MSG_NOTIFICATION_CARD,
    MSG_PERSONAL_CARD,
    MSG_RED_ENVELOPE,
    MSG_RICH_TEXT,
    MSG_REVOKE_NOTICE,
    MSG_ROOM_INVITE,
    MSG_SECURITY_CENTER,
    MSG_SNS_TASK,
    MSG_SOLITAIRE,
    MSG_TEXT,
    MSG_TIPS,
    MSG_TYPE_NAMES,
    MSG_VIDEO,
    MSG_VIDEO_CDN,
    MSG_VIDEO_FULL_CDN,
    MSG_VOICE,
    MSG_VOICE_CDN,
    MSG_WECHAT_EMOTION,
    MSG_WECHAT_FILE,
    MSG_WECHAT_IMAGE,
    MSG_WECHAT_VIDEO,
    MSG_WECHAT_VOICE,
)

# 只处理真正的人类会话：群聊（roomId 非空）或私聊（roomId 为空）。
# display.category 的完整枚举（QWPad OpenAPI RealtimeDisplay）：
#   chat=聊天 / system=系统通知 / account=设备、账号状态 / unknown=待解析通知
# 后三者一律不得进入 AstrBot —— 这里在解析层直接丢弃。
CHAT_CATEGORY = "chat"
EVENT_TYPE_CHAT = 15000

MEDIA_MSG_TYPES = {
    MSG_IMAGE: ("image", FILE_TYPE_IMAGE),
    MSG_CONTENT_IMAGE: ("image", FILE_TYPE_IMAGE),
    MSG_CONTENT_IMAGE_CDN: ("image", FILE_TYPE_IMAGE),
    MSG_CONTENT_IMAGE_WWFTN: ("image", FILE_TYPE_IMAGE),
    MSG_CONTENT_IMAGE_WWFTN_ENCRYPT: ("image", FILE_TYPE_IMAGE),
    MSG_CONTENT_IMAGE_P2P: ("image", FILE_TYPE_IMAGE),
    MSG_EMOTION: ("image", FILE_TYPE_IMAGE),
    MSG_WECHAT_IMAGE: ("image", FILE_TYPE_IMAGE),
    MSG_WECHAT_EMOTION: ("image", FILE_TYPE_IMAGE),
    MSG_VOICE: ("voice", FILE_TYPE_FILE),
    MSG_VOICE_CDN: ("voice", FILE_TYPE_FILE),
    MSG_CONTENT_VOICE_WWFTN: ("voice", FILE_TYPE_FILE),
    MSG_CONTENT_VOICE_WWFTN_ENCRYPT: ("voice", FILE_TYPE_FILE),
    MSG_WECHAT_VOICE: ("voice", FILE_TYPE_FILE),
    MSG_VIDEO: ("video", FILE_TYPE_VIDEO),
    MSG_VIDEO_CDN: ("video", FILE_TYPE_VIDEO),
    MSG_VIDEO_FULL_CDN: ("video", FILE_TYPE_VIDEO),
    MSG_CONTENT_VIDEO_WWFTN: ("video", FILE_TYPE_VIDEO),
    MSG_CONTENT_VIDEO_WWFTN_ENCRYPT: ("video", FILE_TYPE_VIDEO),
    MSG_WECHAT_VIDEO: ("video", FILE_TYPE_VIDEO),
    MSG_FILE: ("file", FILE_TYPE_FILE),
    MSG_CONTENT_FILE: ("file", FILE_TYPE_FILE),
    MSG_CONTENT_FILE_CDN: ("file", FILE_TYPE_FILE),
    MSG_CONTENT_FILE_WWFTN: ("file", FILE_TYPE_FILE),
    MSG_CONTENT_FILE_WWFTN_ENCRYPT: ("file", FILE_TYPE_FILE),
    MSG_WECHAT_FILE: ("file", FILE_TYPE_FILE),
}

# 个人微信（imunion 直链）媒体：没有 fileId，走 /cloud/wxDownload。
EXTERNAL_MEDIA_MSG_TYPES = {MSG_WECHAT_IMAGE, MSG_WECHAT_FILE, MSG_WECHAT_VIDEO}

# 这些类型是纯噪音：已读回执、撤回提示等，不进上下文、不回复。
IGNORED_MSG_TYPES = {2001, 2005, MSG_REVOKE_NOTICE, 2180}

PLACEHOLDERS = {"image": "[图片]", "voice": "[语音]", "video": "[视频]", "file": "[文件]"}


@dataclass
class MediaRef:
    """收到消息里的媒体凭证，下载时才用。"""

    kind: str  # image | voice | video | file
    file_id: str = ""
    aes_key: str = ""
    size: int = 0
    md5: str = ""
    filename: str = ""
    file_type: int = FILE_TYPE_FILE
    duration: int = 0
    has_hd: bool = False
    # 个人微信媒体（msgType 101 kWechatImage）没有 fileId，只能拿「直接地址 + 鉴权值」
    # 走 /cloud/wxDownload；按 big → middle → thumb 顺序尝试。
    auth_key: str = ""
    wx_candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fileId": self.file_id,
            "aesKey": self.aes_key,
            "size": self.size,
            "fileSize": self.size,
            "md5": self.md5,
            "filename": self.filename,
            "fileType": self.file_type,
            "duration": self.duration,
            "hasHd": self.has_hd,
            "fileAuthKey": self.auth_key,
            "wxCandidates": self.wx_candidates,
        }

    @property
    def downloadable(self) -> bool:
        return bool(self.file_id or self.wx_candidates)


@dataclass
class InboundMessage:
    """一条通过校验、准备交给 AstrBot 的入站消息。"""

    event_id: int
    message_id: str
    msg_type: int
    msg_name: str
    chat_id: str
    chat_type: str  # group | dm
    user_id: str
    user_name: str
    text: str
    mentions_me: bool = False
    is_self: bool = False
    is_revoked: bool = False
    media: MediaRef | None = None
    # 聊天记录（kForward）里内嵌的图片/文件；顶层消息本身没有媒体，
    # 只有 subMsgList 里的媒体子消息，下载后一起作为附件交给模型。
    media_refs: list[MediaRef] = field(default_factory=list)
    reply: dict[str, Any] | None = None
    timestamp: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def placeholder(self) -> str:
        return PLACEHOLDERS.get(self.media.kind, "") if self.media else ""


def _s(value: Any) -> str:
    """把各种 ID 形态统一成字符串；None/0 变空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("0", "None", "null") else text


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value:
            return str(value)
    return ""


def _extract_media(
    data: dict[str, Any], display: dict[str, Any], msg_type: int
) -> MediaRef | None:
    kind, file_type = MEDIA_MSG_TYPES.get(msg_type, ("", FILE_TYPE_FILE))
    if not kind:
        return None
    content = _as_dict(display.get("content"))
    media_meta = _as_dict(content.get("media"))
    # 部分消息（语音/视频/文件）把标识放在 fileInfo 里。
    file_info = _as_dict(data.get("fileInfo"))
    file_id = _first_str(data, "fileId", "fileid", "cdnFileId") or _first_str(
        file_info, "fileId", "thumbnailFileId"
    )
    aes_key = _first_str(
        data, "fileAeskey", "fileAesKey", "fileKey", "aesKey", "fileaeskey"
    ) or _first_str(file_info, "aesKey", "openimCdnLdAesKey")
    if not file_id:
        # 部分消息只在 display.content 里给 id/url
        file_id = _first_str(media_meta, "fileId")
        aes_key = aes_key or _first_str(
            media_meta, "fileAesKey", "fileAeskey", "fileKey"
        )
    size = _int(
        data.get("fileSize")
        or data.get("size")
        or media_meta.get("sizeBytes")
        or file_info.get("size")
    )
    md5 = (
        _first_str(data, "fileMd5", "md5")
        or _first_str(media_meta, "md5")
        or _first_str(file_info, "md5")
    )
    filename = (
        _first_str(data, "filename", "fileName", "title")
        or _first_str(media_meta, "fileName")
        or _first_str(content, "title")
    )
    duration = _int(
        data.get("duration")
        or data.get("durationTime")
        or data.get("voiceTime")
        or media_meta.get("durationSeconds")
        or file_info.get("voiceTime")
    )
    has_hd = bool(data.get("imageHasHd") or file_info.get("isHd"))
    if kind == "image" and file_id and "imageHasHd" in data:
        # openapi：image_has_hd=1 用 fileType=1（大图），=0 用 2（小图）。
        file_type = FILE_TYPE_IMAGE if has_hd else 2
    ref = MediaRef(
        kind=kind,
        file_id=file_id,
        aes_key=aes_key,
        size=size,
        md5=md5,
        filename=filename,
        file_type=file_type,
        duration=duration,
        has_hd=has_hd,
    )
    if msg_type in EXTERNAL_MEDIA_MSG_TYPES:
        # 个人微信媒体：没有 fileId，只有 imunion 直链 + fileAuthKey，
        # 走 /cloud/wxDownload。
        ref.auth_key = _first_str(data, "fileAuthKey", "fileAuthkey", "authKey")
        candidates: list[dict[str, Any]] = []
        if msg_type == MSG_WECHAT_IMAGE:
            # 2026-10-22 实测：原图/中图在 QWPad 侧转存 502，缩略图可用，所以按序回退。
            for url_key, size_key, cand_type in (
                ("fileBigHttpUrl", "fileBigSize", 1),
                ("fileMiddleHttpUrl", "fileMiddleSize", 2),
                ("fileThumbHttpUrl", "fileThumbSize", 3),
            ):
                url = _first_str(data, url_key)
                if url:
                    candidates.append(
                        {
                            "fileUrl": url,
                            "fileSize": _int(data.get(size_key)),
                            "fileType": cand_type,
                        }
                    )
        else:
            url = _first_str(data, "fileHttpUrl", "fileUrl")
            if url:
                candidates.append(
                    {
                        "fileUrl": url,
                        "fileSize": size,
                        "fileType": 4 if kind == "video" else FILE_TYPE_FILE,
                    }
                )
        ref.wx_candidates = candidates
        if not ref.size and candidates:
            ref.size = int(candidates[0].get("fileSize") or 0)
        if not ref.md5:
            ref.md5 = _first_str(data, "fileMd5", "md5")
    # 没有 fileId / 直链就没法下载——仍然返回，让上层用占位文本
    return ref


def _clean_text(text: str, self_nick: str) -> str:
    """去掉 @自己 的前缀/行内标记。"""
    if not text:
        return ""
    cleaned = text.replace(" ", " ").replace("​", "")
    for nick in {n for n in (self_nick, "@" + self_nick) if n}:
        if cleaned.startswith(nick + " "):
            cleaned = cleaned[len(nick) + 1 :]
        elif cleaned.startswith(nick + " "):
            cleaned = cleaned[len(nick) + 1 :]
        cleaned = (
            cleaned.replace(nick + " ", "", 1)
            if cleaned.startswith(nick)
            else cleaned
        )
    return cleaned.strip()


def _is_mentioned(
    data: dict[str, Any], text: str, self_id: str, self_nick: str
) -> bool:
    at_list = data.get("atList") or data.get("atlist")
    if isinstance(at_list, list):
        for item in at_list:
            entry = _as_dict(item)
            if self_id and _s(entry.get("userId")) == self_id:
                return True
            if self_nick and _s(entry.get("nickname")) == self_nick:
                return True
    if self_nick and (f"@{self_nick}" in (text or "")):
        return True
    return False


# 人工在企微客户端「引用」一条消息时，pad 服务端会把引用压平成纯文本（实测 2026-10-22）：
#   `"原文"\n------\n新内容`，msgType 降为 kText，结构化 `reply=null`、`referId="0"`。
# 这里把它还原成「引用正文 + 新正文」，否则模型会把引用的原话当成用户本次说的话。
_FLATTENED_QUOTE_SEP = re.compile(r"\n\s*-{3,}\s*\n")
_QUOTE_WRAPPERS = (('"', '"'), ("“", "”"), ("「", "」"))


def _unwrap_quote(text: str) -> str | None:
    """``"原文"`` / ``“原文”`` / ``「原文」`` → 原文；不像引用则返回 None。"""
    head = text.strip()
    if len(head) < 2:
        return None
    for left, right in _QUOTE_WRAPPERS:
        if head.startswith(left) and head.endswith(right):
            return head[len(left) : -len(right)].strip() or None
    return None


def split_flattened_quote(text: str) -> tuple[str | None, str]:
    """拆 ``"原文"\\n------\\n新内容`` → ``(原文, 新内容)``；不是这种形态返回 ``(None, text)``。"""
    if not text:
        return None, text
    parts = _FLATTENED_QUOTE_SEP.split(text, maxsplit=1)
    if len(parts) != 2:
        return None, text
    quote = _unwrap_quote(parts[0])
    body = parts[1].strip()
    if not quote or not body:  # 引用原文为空或只有引用没正文 —— 不拆，保持原样
        return None, text
    return quote, body


def strip_html(text: str) -> str:
    """企业微信部分卡片正文是 HTML 片段 —— 去掉标签、压空白。"""
    if "<" not in text or ">" not in text:
        return text
    plain = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", " ", plain)
    plain = re.sub(r"[ \t]{2,}", " ", plain)
    return re.sub(r"[ \t]+\n", "\n", plain).strip()


def _json_title(raw: str) -> str:
    """通知卡片常把可读标题塞在 JSON 字符串里（``header.title``）。"""
    if not raw or "{" not in raw:
        return ""
    start = raw.find("{")
    try:
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    header = _as_dict(value.get("header"))
    return _first_str(header, "title") or _first_str(value, "title")


def _sub_message_text(sub_type: int, data: dict[str, Any]) -> str:
    """聊天记录/图文组合里的单个子消息。"""
    if sub_type in (MSG_FORWARD, MSG_MIXED):
        return "[聊天记录]"
    text = _extract_text(data, {}, sub_type)
    if text:
        return text
    kind = MEDIA_MSG_TYPES.get(sub_type, ("",))[0]
    if kind == "file":
        name = _first_str(data, "filename", "fileName")
        return f"[文件] {name}".strip()
    return PLACEHOLDERS.get(kind, "")


def _combined_text(data: dict[str, Any]) -> str:
    desc = _first_str(data, "subMsgDesc")
    lines: list[str] = [desc] if desc else []
    subs = data.get("subMsgList")
    if isinstance(subs, list):
        for sub in subs[:20]:
            entry = _as_dict(sub)
            body = _sub_message_text(
                _int(entry.get("subMsgType")), _as_dict(entry.get("subMsgData"))
            )
            if not body:
                continue
            sender = _first_str(entry, "subMsgSenderName")
            lines.append(f"{sender}: {body}" if sender else body)
    return "\n".join(["[聊天记录]"] + lines)[:2000]


def _forward_media_refs(data: dict[str, Any]) -> list[MediaRef]:
    """聊天记录里内嵌的媒体子消息（实测「接晓银和…的聊天记录」里 subMsgType=101）。

    ``_combined_text`` 只把它们渲染成 ``[图片]``/``[文件]`` 占位；这里把下载凭证
    一并带出来，交给适配器下载后作为附件，否则模型只能看到文字占位。
    """
    refs: list[MediaRef] = []
    subs = data.get("subMsgList")
    if not isinstance(subs, list):
        return refs
    for sub in subs:
        if len(refs) >= 10:  # 单条消息的附件上限同量级
            break
        entry = _as_dict(sub)
        sub_type = _int(entry.get("subMsgType"))
        if sub_type in (MSG_FORWARD, MSG_MIXED):
            continue  # 不递归展开嵌套聊天记录
        ref = _extract_media(_as_dict(entry.get("subMsgData")), {}, sub_type)
        if ref and ref.downloadable:
            refs.append(ref)
    return refs


def _extract_text(data: dict[str, Any], display: dict[str, Any], msg_type: int) -> str:
    """按 openapi 的 ``WW*MessageBody`` 结构提取可读正文。

    ``display.content`` 是服务端给所有类型做的统一视图（text/title/description/url/media），
    结构化的 ``msgData`` 用来补足它丢掉的字段（红包祝福语、名片、接龙、回调 JSON 等）。
    """
    content_block = _as_dict(display.get("content"))

    if msg_type in (MSG_TEXT, MSG_RICH_TEXT):
        text = (
            _first_str(data, "content", "text")
            or _first_str(display, "text")
            or _first_str(content_block, "text")
        )
        return strip_html(text)

    if msg_type == MSG_APP_TEXT_CARD:
        title = _first_str(data, "title") or _first_str(content_block, "title")
        body = strip_html(
            _first_str(data, "content", "text") or _first_str(display, "text")
        )
        url = _first_str(data, "url") or _first_str(content_block, "url")
        parts = [p for p in (f"[卡片] {title}".strip(), body, url) if p]
        return "\n".join(parts)

    if msg_type == MSG_LINK:
        title = _first_str(data, "title") or _first_str(content_block, "title")
        url = _first_str(data, "linkUrl", "url") or _first_str(content_block, "url")
        desc = _first_str(data, "desc", "description") or _first_str(
            content_block, "description"
        )
        return " — ".join(p for p in (title, desc, url) if p)

    if msg_type == MSG_LOCATION:
        title = _first_str(data, "title", "name") or _first_str(content_block, "title")
        address = _first_str(data, "address") or _first_str(
            content_block, "description"
        )
        lat, lon = data.get("latitude"), data.get("longitude")
        where = " ".join(p for p in (title, address) if p)
        coords = (
            f"({lat},{lon})"
            if lat not in (None, "") and lon not in (None, "")
            else ""
        )
        return f"[位置] {where} {coords}".strip()

    if msg_type in (MSG_FORWARD, MSG_MIXED):
        return _combined_text(data)

    if msg_type == MSG_INVITE_RECORD:
        wording = _first_str(data, "recordWording")
        return f"[通话记录] {wording}".strip()

    if msg_type == MSG_RED_ENVELOPE:
        wish = _first_str(data, "wishingContent")
        amount = data.get("totalAmount")
        parts = ["[红包]", wish]
        if isinstance(amount, (int, float)) and amount:
            # openapi: 「保留返回单位，不作货币换算」——不猜元/分，只标原始值。
            parts.append(f"金额原始值={amount}")
        return " ".join(str(p) for p in parts if p).strip()

    if msg_type == MSG_PERSONAL_CARD:
        name = _first_str(data, "realName", "nickname")
        nick = _first_str(data, "nickname")
        corp = _first_str(data, "corpName")
        detail = " / ".join(dict.fromkeys(p for p in (name, nick, corp) if p))
        return f"[名片] {detail}".strip()

    if msg_type == MSG_MINI_PROGRAM:
        title = _first_str(data, "title") or _first_str(content_block, "title")
        desc = _first_str(data, "desc") or _first_str(
            content_block, "description"
        )
        app = _first_str(data, "appName")
        parts = [p for p in (title, desc, app) if p]
        return f"[小程序] {' — '.join(parts)}".strip()

    if msg_type in (MSG_EMOTION, MSG_WECHAT_EMOTION):
        source = _first_str(data, "sourceDesc")
        return f"[表情] {source}".strip()

    if msg_type == MSG_CHANNEL:
        name = _first_str(data, "channelName")
        url = _first_str(data, "channelUrl")
        return " — ".join(p for p in ("[视频号]", name, url) if p)

    if msg_type == MSG_SOLITAIRE:
        title = _first_str(data, "title")
        return f"[接龙] {title}".strip()

    if msg_type in (MSG_SECURITY_CENTER, 2166):
        wording = _first_str(data, "wording")
        return f"[通话] {wording}".strip() if wording else "[通话通知]"

    if msg_type == MSG_SNS_TASK:
        title = _first_str(data, "snsTaskTitle")
        kind = _first_str(data, "snsTaskType")
        return " — ".join(p for p in ("[朋友圈任务]", title, kind) if p)

    if msg_type == MSG_INNER_ROOM_NOTICE:
        notice = _as_dict(data.get("innerRoomNotice"))
        return f"[群通知] {_first_str(notice, 'content')}".strip()

    if msg_type == MSG_NOTIFICATION_CARD:
        title = _first_str(data, "notifyTitle")
        desc = _first_str(data, "notifyMsgDesc")
        body = _first_str(data, "notifyContent") or _json_title(
            _first_str(data, "notifyRichContent")
        )
        parts = [p for p in (title, desc, body) if p]
        return "\n".join(["[通知]"] + parts) if parts else ""

    if msg_type == MSG_TIPS:
        tips = data.get("tipsList")
        lines = []
        if isinstance(tips, list):
            for item in tips:
                line = _first_str(_as_dict(item), "tips")
                if line:
                    lines.append(line)
        return "\n".join(["[群提示]"] + lines)

    if msg_type == MSG_ROOM_INVITE:
        desc = _first_str(data, "inviteDesc")
        return f"[群邀请] {desc}".strip()

    if msg_type == MSG_MEMBER_OPERATION:
        members = data.get("vids")
        count = len(members) if isinstance(members, list) else 0
        return f"[成员管理] type={data.get('type')} 涉及 {count} 人".strip()

    if msg_type in (1001, 1002, 1003, 1004, 1006):
        changed = _first_str(data, "changedMemberList")
        return f"[群成员变化] {changed}".strip()

    if msg_type == 2357:  # 好友申请（通常走旁路留存，不回复）
        who = _first_str(data, "contactNickname")
        return f"[好友申请] {who}".strip()

    # 通用回退：display.content 统一视图 → msgData 常见字段 → decodedRawData。
    text = strip_html(_first_str(display, "text"))
    if not text:
        text = _first_str(content_block, "text")
    if not text:
        title = _first_str(content_block, "title") or _first_str(data, "title")
        desc = _first_str(content_block, "description") or _first_str(
            data, "desc", "description", "subMsgDesc"
        )
        url = _first_str(content_block, "url") or _first_str(data, "url", "linkUrl")
        parts = [p for p in (title, desc, url) if p]
        text = "\n".join(dict.fromkeys(parts))
    if not text:
        text = strip_html(_first_str(data, "content", "text", "notifyContent"))
    if not text:
        decoded = _as_dict(data.get("decodedRawData"))
        if decoded.get("format") in ("utf8", "json", "xml"):
            text = _first_str(decoded, "text") or (
                json.dumps(decoded.get("value"), ensure_ascii=False)
                if decoded.get("value") is not None
                else ""
            )
    return strip_html(text)


def parse_event(
    item: dict[str, Any], *, self_id: str = "", self_nick: str = ""
) -> InboundMessage | None:
    """把一条 unified 事件映射成 ``InboundMessage``；无关事件返回 ``None``。"""
    if not isinstance(item, dict):
        return None
    message = _as_dict(item.get("message"))
    structured = _as_dict(message.get("structured"))
    display = _as_dict(item.get("display"))
    data = _as_dict(structured.get("msgData")) or _as_dict(message.get("msgData"))

    # 会话类型硬闸：只放行 chat（群聊/私聊）。system / account / unknown 一律丢弃。
    # 放在任何内容解析之前，与鉴权无关 —— 放行模式下也绕不过去。
    event_type = _int(item.get("type"))
    if event_type and event_type != EVENT_TYPE_CHAT:
        logger.debug("[QWPad] 跳过非聊天事件 type=%s", event_type)
        return None
    category = str(display.get("category") or "").strip().lower()
    if category != CHAT_CATEGORY:
        logger.debug(
            "[QWPad] 跳过非聊天事件 category=%s sender=%s room=%s",
            category or "<missing>",
            _s(display.get("senderId")),
            _s(display.get("roomId")),
        )
        return None

    msg_type = _int(message.get("msgType") or structured.get("msgType"))
    if msg_type in IGNORED_MSG_TYPES:
        return None

    # openapi 的 display.messageType 带服务端生成的 nativeName / parseStatus，
    # 比本地常量表更全（类型目录 578 项），优先用它。
    type_meta = _as_dict(display.get("messageType"))
    native_name = _s(type_meta.get("nativeName"))
    parse_status = str(display.get("parseStatus") or "").strip().lower()

    sender_id = _s(structured.get("senderId")) or _s(display.get("senderId"))
    room_id = _s(structured.get("fromRoomId")) or _s(display.get("roomId"))
    is_revoked = bool(structured.get("isRevoked")) or bool(message.get("isRevoked"))

    if not sender_id and not room_id:
        return None

    text = _extract_text(data, display, msg_type)
    media = _extract_media(data, display, msg_type)
    media_refs = (
        _forward_media_refs(data) if msg_type in (MSG_FORWARD, MSG_MIXED) else []
    )

    # 结构化 reply 优先；没有就尝试还原被压平的 `"原文"\n------\n新内容`
    reply = _as_dict(data.get("reply")) or None
    if reply is None:
        flat_quote, text = split_flattened_quote(text)
        if flat_quote:
            reply = {"msgData": {"content": flat_quote}, "flattened": True}
            logger.debug(
                "[QWPad] 入站引用被压平，已还原引用正文 len=%s", len(flat_quote)
            )
    chat_type = "group" if room_id else "dm"
    chat_id = room_id or sender_id

    inbox = InboundMessage(
        event_id=_int(item.get("id")),
        message_id=(
            _s(structured.get("msgUniqueIdentifier"))
            or _s(display.get("messageId"))
            or f"evt-{_int(item.get('id'))}"
        ),
        msg_type=msg_type,
        msg_name=native_name or MSG_TYPE_NAMES.get(msg_type, f"msgType:{msg_type}"),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=sender_id,
        user_name=(
            _s(structured.get("senderName"))
            or _s(display.get("senderName"))
            or sender_id
        ),
        text=_clean_text(text, self_nick),
        mentions_me=_is_mentioned(data, text, self_id, self_nick),
        is_self=bool(self_id and sender_id == self_id),
        is_revoked=is_revoked,
        media=media,
        media_refs=media_refs,
        reply=reply,
        timestamp=_int(structured.get("timestamp")),
        raw=item,
    )
    if inbox.media and not inbox.text:
        inbox.text = inbox.placeholder
    if is_revoked:
        return None
    if not inbox.text and not (inbox.media and inbox.media.downloadable):
        # 578 种 msgType 里只有 58 种有具名 body。解析器没覆盖、但服务端明确
        # 有正文的类型，用 nativeName 占位进对话（模型至少能告知「收到一条 X」）；
        # no_body / empty_body 才是纯噪音。
        label = native_name or MSG_TYPE_NAMES.get(msg_type, "")
        if label and parse_status in ("parsed", "parsed_media", "partial", "unsupported"):
            inbox.text = f"[{label}]"
        else:
            logger.debug(
                "[QWPad] 丢弃无可读正文的消息 msgType=%s parseStatus=%s",
                msg_type,
                parse_status or "<missing>",
            )
            return None
    return inbox


def reply_context(inbox: InboundMessage) -> str | None:
    """引用消息的正文（放进 ``Reply.message_str`` 用）。"""
    reply = _as_dict(inbox.reply)
    if not reply:
        return None
    body = _as_dict(reply.get("msgData"))
    text = _first_str(body, "content", "text")
    return text or None


__all__ = [
    "CHAT_CATEGORY",
    "EVENT_TYPE_CHAT",
    "IGNORED_MSG_TYPES",
    "MEDIA_MSG_TYPES",
    "InboundMessage",
    "MediaRef",
    "PLACEHOLDERS",
    "parse_event",
    "reply_context",
    "split_flattened_quote",
    "strip_html",
]
