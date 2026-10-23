"""QWPad（企业微信 pad 协议）HTTP 客户端。

一个 ``wxa_`` TOKEN 对应一个企业微信账号；所有业务接口走
``Authorization: Bearer <TOKEN>``。响应统一为 ``{code, msg, data}``，
``code == 0`` 才算成功。

接口文档：https://api.qwpad.com/openapi.json
协议实现参考 Hermes qwpad 插件（已在生产验证的路径与字段）。
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import httpx
from astrbot import logger

DEFAULT_BASE_URL = "https://api.qwpad.com"
DEFAULT_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 180.0
BIG_UPLOAD_TIMEOUT = 600.0
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
# 普通 CDN 上传必须非空且小于 20MiB（openapi /cloud/cdnBigUpload）。
MAX_CDN_UPLOAD_BYTES = 20 * 1024 * 1024
# ``/cloud/files`` 签名地址只认浏览器 UA，httpx 默认 UA 会 403（2026-10-22 实测）。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/126.0 Safari/537.36"
)

# 普通上传 fileType 枚举（openapi）：1 图片 JPG / 4 视频 MP4 / 5 普通文件或 SILK 语音
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 4
FILE_TYPE_FILE = 5
# 大文件上传 fileType 枚举（openapi /cloud/cdnBigFileUpload）：22 视频 / 35 文件
BIG_FILE_TYPE_VIDEO = 22
BIG_FILE_TYPE_FILE = 35

# msgType（/msg/getMessageTypeList）——只列适配器关心的
MSG_TEXT = 0
MSG_IMAGE = 1
MSG_RICH_TEXT = 2
MSG_FILE = 3
MSG_FORWARD = 4
MSG_VIDEO = 5
MSG_LOCATION = 6
MSG_CONTENT_IMAGE = 7
MSG_CONTENT_FILE = 8
MSG_VOICE = 9
MSG_APP_CONTENT = 10
MSG_LINK = 13
MSG_CONTENT_IMAGE_CDN = 14
MSG_CONTENT_FILE_CDN = 15
MSG_VOICE_CDN = 16
MSG_VIDEO_CDN = 17
MSG_CONTENT_IMAGE_WWFTN = 19
MSG_CONTENT_FILE_WWFTN = 20
MSG_CONTENT_VOICE_WWFTN = 21
MSG_CONTENT_VIDEO_WWFTN = 22
MSG_VIDEO_FULL_CDN = 23
MSG_APP_CARD = 25
MSG_RED_ENVELOPE = 26
MSG_EMOTION = 29
MSG_APP_NEWS = 35
MSG_EXTERNAL_FORWARD = 36
MSG_APP_RICH_MSG = 37
MSG_APP_TEXT_CARD = 38
MSG_INVITE_RECORD = 40
MSG_PERSONAL_CARD = 41
MSG_CLOUD_DISK = 42
MSG_APP_TASK_CARD = 46
MSG_CONTENT_IMAGE_WWFTN_ENCRYPT = 48
MSG_CONTENT_FILE_WWFTN_ENCRYPT = 49
MSG_CONTENT_VOICE_WWFTN_ENCRYPT = 50
MSG_CONTENT_VIDEO_WWFTN_ENCRYPT = 51
MSG_INVOICE = 57
MSG_CORP_PAY = 61
MSG_CONTENT_IMAGE_P2P = 63
MSG_MINI_PROGRAM = 78
MSG_MARKDOWN = 80
MSG_COLLECT_CARD = 86
MSG_MEETING_INVITE = 87
MSG_ROBOT_CARD = 88
MSG_WECHAT_IMAGE = 101
MSG_WECHAT_FILE = 102
MSG_WECHAT_VIDEO = 103
MSG_WECHAT_EMOTION = 104
MSG_MIXED = 123
MSG_CHANNEL = 141
MSG_SOLITAIRE = 213
MSG_WECHAT_VOICE = 216
MSG_SECURITY_CENTER = 503
MSG_SNS_TASK = 529
MSG_INNER_ROOM_NOTICE = 570
MSG_NOTIFICATION_CARD = 573
MSG_TIPS = 1022
MSG_ROOM_INVITE = 1029
MSG_MEMBER_OPERATION = 1043
MSG_REVOKE_NOTICE = 2063

MSG_TYPE_NAMES = {
    MSG_TEXT: "kText",
    MSG_IMAGE: "kImage",
    MSG_RICH_TEXT: "kRichText",
    MSG_FILE: "kFile",
    MSG_FORWARD: "kForward",
    MSG_VIDEO: "kVideo",
    MSG_LOCATION: "kLocation",
    MSG_CONTENT_IMAGE: "kContentImage",
    MSG_CONTENT_FILE: "kContentFile",
    MSG_VOICE: "kVoice",
    MSG_APP_CONTENT: "kAppContent",
    MSG_LINK: "kLink",
    MSG_CONTENT_IMAGE_CDN: "kContentImageCDN",
    MSG_CONTENT_FILE_CDN: "kContentFileCDN",
    MSG_VOICE_CDN: "kVoiceCDN",
    MSG_VIDEO_CDN: "kVideoCDN",
    MSG_CONTENT_IMAGE_WWFTN: "kContentImageWWFTN",
    MSG_CONTENT_FILE_WWFTN: "kContentFileWWFTN",
    MSG_CONTENT_VOICE_WWFTN: "kContentVoiceWWFTN",
    MSG_CONTENT_VIDEO_WWFTN: "kContentVideoWWFTN",
    MSG_VIDEO_FULL_CDN: "kVideoFullCDN",
    MSG_APP_CARD: "kAppCardMsg",
    MSG_RED_ENVELOPE: "kHongBaoMsg",
    MSG_EMOTION: "kEmotion",
    MSG_APP_NEWS: "kAppNews",
    MSG_EXTERNAL_FORWARD: "kExternalForward",
    MSG_APP_RICH_MSG: "kAppRichMsg",
    MSG_APP_TEXT_CARD: "kAppTextCard",
    MSG_INVITE_RECORD: "kPVMergeSingleMsg",
    MSG_PERSONAL_CARD: "kPersonalCard",
    MSG_CLOUD_DISK: "kCloudDisk",
    MSG_APP_TASK_CARD: "kAppTaskCard",
    MSG_CONTENT_IMAGE_WWFTN_ENCRYPT: "kContentImageWWFTNEncrypt",
    MSG_CONTENT_FILE_WWFTN_ENCRYPT: "kContentFileWWFTNEncrypt",
    MSG_CONTENT_VOICE_WWFTN_ENCRYPT: "kContentVoiceWWFTNEncrypt",
    MSG_CONTENT_VIDEO_WWFTN_ENCRYPT: "kContentVideoWWFTNEncrypt",
    MSG_INVOICE: "kInvoice",
    MSG_CORP_PAY: "kCorpPay",
    MSG_CONTENT_IMAGE_P2P: "kContentImageP2P",
    MSG_MINI_PROGRAM: "kWeAPPMsg",
    MSG_MARKDOWN: "kMarkDownMsg",
    MSG_COLLECT_CARD: "kCollectCardMsg",
    MSG_MEETING_INVITE: "kMeetingInviteMsg",
    MSG_ROBOT_CARD: "kContentRobotCard",
    MSG_WECHAT_IMAGE: "kWechatImage",
    MSG_WECHAT_FILE: "kWechatFile",
    MSG_WECHAT_VIDEO: "kWechatVideo",
    MSG_WECHAT_EMOTION: "kWechatEmotion",
    MSG_MIXED: "kMixedMsg",
    MSG_CHANNEL: "kWechatSphFeed",
    MSG_SOLITAIRE: "E_CONTENT_SOLITAIRE_MSG",
    MSG_WECHAT_VOICE: "kWechatVoice",
    MSG_SECURITY_CENTER: "kPVMerge_Push",
    MSG_SNS_TASK: "kSnsNotify",
    MSG_INNER_ROOM_NOTICE: "E_CONTENT_ROOM_DOC_NOTICE",
    MSG_NOTIFICATION_CARD: "E_CONTENT_FUN_VIEW_DATA_TIPS_NEW",
    MSG_TIPS: "kRoomChangeTips",
    MSG_ROOM_INVITE: "kConfirmAddMember",
    MSG_MEMBER_OPERATION: "kRoomAdminModify",
    MSG_REVOKE_NOTICE: "kRevokeMsg",
}


class QWPadError(Exception):
    """QWPad 调用失败的基类。``retryable`` 决定轮询循环是否退避重试。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class QWPadAuthError(QWPadError):
    """TOKEN 缺失/过期/未绑定——重试无用。"""

    def __init__(
        self, message: str, *, status: int | None = None, code: int | None = None
    ) -> None:
        super().__init__(message, status=status, code=code, retryable=False)


class QWPadApiError(QWPadError):
    """业务码非 0 / HTTP 4xx。"""


@dataclass
class EventsPage:
    """``GET /events`` 的一页结果。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 0
    has_more: bool = False


def _md5_file(path: str) -> str:
    """本地计算 MD5；上传响应缺 fileMd5 时兜底（发送接口要求该字段）。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@asynccontextmanager
async def _open_binary(path: str) -> AsyncIterator[BinaryIO]:
    """在工作线程打开文件，避免上传前阻塞事件循环。

    httpx 的 multipart 仍会同步读取文件句柄，但打开/关闭以及 MD5 兜底计算
    已经移出事件循环；大文件上传本身是网络 I/O，句柄读取占比很小。
    """
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        yield fh
    finally:
        await asyncio.to_thread(fh.close)


def _unwrap(payload: Any, path: str) -> Any:
    """解包 ``{code,msg,data}``；成功返回 ``data``。"""
    if not isinstance(payload, dict):
        raise QWPadApiError(
            f"{path}: 响应不是 JSON 对象: {type(payload).__name__}", retryable=False
        )
    code = payload.get("code")
    if code == 0:
        return payload.get("data")
    msg = str(payload.get("msg") or "")
    # 401400 认证失败 / TOKEN 未启用：不要重试
    if code in (401, 401400, 401403) or "认证" in msg:
        raise QWPadAuthError(
            f"{path}: {code} {msg}", code=code if isinstance(code, int) else None
        )
    raise QWPadApiError(
        f"{path}: {code} {msg}", code=code if isinstance(code, int) else None
    )


class QWPadClient:
    """一个 TOKEN 对应一个企业微信账号的异步客户端。"""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        # 下载走独立客户端：cloudUrl 是对象存储地址，绝不能带业务 TOKEN。
        self._download_http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._download_lock = asyncio.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def __aenter__(self) -> QWPadClient:  # noqa: PYI034 - py3.10 无 typing.Self
        await self._ensure_http()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is not None and not self._http.is_closed:
            return self._http
        async with self._lock:
            if self._http is None or self._http.is_closed:
                self._http = httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                        "User-Agent": "AstrBot-qwpad/1.0",
                    },
                    follow_redirects=True,
                    transport=self._transport,
                )
        return self._http

    async def _ensure_download_http(self) -> httpx.AsyncClient:
        """独立下载客户端：不继承 base_url / Authorization。"""
        if self._download_http is not None and not self._download_http.is_closed:
            return self._download_http
        async with self._download_lock:
            if self._download_http is None or self._download_http.is_closed:
                self._download_http = httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    headers={"User-Agent": BROWSER_UA},
                    follow_redirects=True,
                    transport=self._transport,
                )
        return self._download_http

    async def close(self) -> None:
        http, self._http = self._http, None
        download_http, self._download_http = self._download_http, None
        for client in (http, download_http):
            if client is None or client.is_closed:
                continue
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("[QWPad] 关闭 HTTP 客户端失败", exc_info=True)

    # ── 底层请求 ────────────────────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        http = await self._ensure_http()
        try:
            response = await http.request(
                method,
                path,
                json=json_body,
                params=params,
                files=files,
                data=data,
                timeout=httpx.Timeout(timeout) if timeout else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.TimeoutException as exc:
            raise QWPadError(f"{path}: 请求超时 ({exc})", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise QWPadError(f"{path}: 网络错误 ({exc})", retryable=True) from exc

        if response.status_code in (401, 403):
            raise QWPadAuthError(
                f"{path}: HTTP {response.status_code}", status=response.status_code
            )
        if response.status_code >= 500:
            raise QWPadError(
                f"{path}: HTTP {response.status_code}",
                status=response.status_code,
                retryable=True,
            )
        if response.status_code >= 400:
            raise QWPadApiError(
                f"{path}: HTTP {response.status_code} {response.text[:200]}",
                status=response.status_code,
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise QWPadError(
                f"{path}: 响应不是合法 JSON", status=response.status_code
            ) from exc
        return _unwrap(payload, path)

    async def post(
        self, path: str, payload: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        return await self._request(
            "POST", path, json_body=payload if payload is not None else {}, **kwargs
        )

    # ── 账号 ────────────────────────────────────────────────────────────────
    async def get_login_status(self) -> dict[str, Any]:
        data = await self.post("/user/getLoginStatus")
        return data or {}

    async def get_profile(self) -> dict[str, Any]:
        data = await self.post("/user/getProfile")
        return data or {}

    async def get_corp_info(self) -> dict[str, Any]:
        return await self.post("/user/getCorpInfo") or {}

    async def get_online_status(self) -> dict[str, Any]:
        return await self.post("/user/getOnlineStatus") or {}

    # ── 收消息 ──────────────────────────────────────────────────────────────
    async def fetch_events(
        self,
        after_id: int = 0,
        *,
        limit: int = 100,
        wait_seconds: int = 25,
        event_type: int | None = None,
        timeout: float | None = None,
    ) -> EventsPage:
        """long-poll 读事件（unified 视图）。"""
        params: dict[str, Any] = {
            "afterId": int(after_id),
            "limit": max(1, min(int(limit), 500)),
            "waitSeconds": max(0, min(int(wait_seconds), 30)),
            "format": "unified",
        }
        if event_type is not None:
            params["type"] = int(event_type)
        data = await self._request(
            "GET",
            "/events",
            params=params,
            timeout=timeout if timeout is not None else float(wait_seconds) + 20.0,
        )
        data = data or {}
        return EventsPage(
            items=list(data.get("items") or []),
            next_id=int(data.get("nextId") or after_id),
            has_more=bool(data.get("hasMore")),
        )

    # ── 发消息 ──────────────────────────────────────────────────────────────
    async def send_text(
        self, to_id: str, content: str, reply: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"toId": str(to_id), "content": content}
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendText", payload) or {}

    async def send_hyper_text(
        self,
        to_id: str,
        content: list[dict[str, Any]],
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /msg/sendHyperText``：正文按 ``[{subtype, text}, ...]`` 分段发送。

        ``subtype``：0 普通文本 / 1 @人的 userId（0 表示 @所有人）/ 2 系统表情。
        """
        payload: dict[str, Any] = {"toId": str(to_id), "content": content}
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendHyperText", payload) or {}

    async def send_image(
        self,
        to_id: str,
        upload: dict[str, Any],
        filename: str,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "fileId": upload.get("fileId"),
            "fileAesKey": upload.get("fileAesKey") or upload.get("fileAeskey") or "",
            "fileSize": int(upload.get("fileSize") or 0),
            "filename": filename,
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendImage", payload) or {}

    async def send_file(
        self,
        to_id: str,
        upload: dict[str, Any],
        filename: str,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "fileId": upload.get("fileId"),
            "fileSize": int(upload.get("fileSize") or 0),
            "fileMd5": upload.get("fileMd5") or "",
            "filename": filename,
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendFile", payload) or {}

    async def send_voice(
        self,
        to_id: str,
        upload: dict[str, Any],
        voice_time: int,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /msg/sendVoice``：fileType=5 上传的 SILK + 秒数。"""
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "fileId": upload.get("fileId"),
            "fileAesKey": upload.get("fileAesKey") or upload.get("fileAeskey") or "",
            "fileSize": int(upload.get("fileSize") or 0),
            "VoiceTime": max(1, int(voice_time)),
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendVoice", payload) or {}

    async def send_video(
        self,
        to_id: str,
        upload: dict[str, Any],
        filename: str,
        *,
        duration: int = 0,
        cover_size: int = 0,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "fileId": upload.get("fileId"),
            "fileAesKey": upload.get("fileAesKey") or upload.get("fileAeskey") or "",
            "fileMd5": upload.get("fileMd5") or "",
            "fileSize": int(upload.get("fileSize") or 0),
            "filename": filename,
            # openapi：duration/coverImageSize 优先复制同次上传的 durationTime/fileThumbSize。
            "duration": max(0, int(upload.get("durationTime") or duration or 0)),
            "coverImageSize": max(
                0, int(upload.get("fileThumbSize") or cover_size or 0)
            ),
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendVideo", payload) or {}

    @staticmethod
    def _big_upload_fields(upload: dict[str, Any]) -> dict[str, Any]:
        """校验 BigCDN 上传结果，返回 sendBigFile/sendBigVideo 的公共字段。"""
        file_id = str(upload.get("fileConvertId") or "").strip()
        if not file_id.startswith("*1*"):
            raise QWPadApiError(
                "大文件上传结果缺少有效的 fileConvertId（*1* 转换 ID）",
                retryable=False,
            )
        md5 = str(upload.get("fileMd5") or "").strip()
        if len(md5) != 32:
            raise QWPadApiError(
                "大文件上传结果缺少有效 fileMd5（32 位十六进制）",
                retryable=False,
            )
        return {
            "fileId": file_id,
            # BigCDN 的 fileKey 不是 AES 密钥；没有 AES 材料时显式填空串。
            "fileAesKey": str(upload.get("fileAesKey") or ""),
            "fileMd5": md5,
            "fileSize": int(upload.get("fileSize") or 0),
        }

    async def send_big_file(
        self,
        to_id: str,
        upload: dict[str, Any],
        filename: str,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /msg/sendBigFile``：发送来自 ``cdnBigFileUpload`` 的文件。"""
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "filename": filename,
            **self._big_upload_fields(upload),
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendBigFile", payload) or {}

    async def send_big_video(
        self,
        to_id: str,
        upload: dict[str, Any],
        filename: str,
        reply: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /msg/sendBigVideo``：发送来自 ``cdnBigFileUpload`` 的视频。"""
        preview = str(upload.get("previewImgUrl") or "")
        payload: dict[str, Any] = {
            "toId": str(to_id),
            "filename": filename,
            "previewImgUrl": preview,
            # openapi：previewImgUrl 为空时 coverImageSize 必须为 0。
            "coverImageSize": int(upload.get("PreviewImgSize") or 0) if preview else 0,
            "duration": max(0, int(upload.get("duration") or 0)),
            **self._big_upload_fields(upload),
        }
        if reply:
            payload["reply"] = reply
        return await self.post("/msg/sendBigVideo", payload) or {}

    def _check_upload_size(self, path: str, file_type: int) -> int:
        if not os.path.isfile(path):
            raise QWPadApiError(f"上传失败：文件不存在 {path}", retryable=False)
        size = os.path.getsize(path)
        if size <= 0:
            raise QWPadApiError(
                f"上传失败：文件为空 {os.path.basename(path)}", retryable=False
            )
        if file_type == FILE_TYPE_IMAGE and size >= MAX_CDN_UPLOAD_BYTES:
            raise QWPadApiError(
                f"图片 {os.path.basename(path)} 为 {size} 字节，超过普通 CDN 上传上限 "
                f"{MAX_CDN_UPLOAD_BYTES} 字节；QWPad 无大图片接口，请先压缩图片",
                retryable=False,
            )
        if (
            file_type in (FILE_TYPE_VIDEO, FILE_TYPE_FILE)
            and size >= MAX_CDN_UPLOAD_BYTES
        ):
            raise QWPadApiError(
                f"文件 {os.path.basename(path)} 为 {size} 字节，超过普通 CDN 上传上限 "
                f"{MAX_CDN_UPLOAD_BYTES} 字节，请改用 upload_big_file",
                retryable=False,
            )
        return size

    @staticmethod
    async def _fill_upload_metadata(
        data: dict[str, Any], path: str, size: int
    ) -> dict[str, Any]:
        if not data.get("fileSize"):
            data["fileSize"] = size
        if not data.get("fileMd5"):
            # 大文件 MD5 可能是数百 MiB 的同步读，必须离开事件循环。
            data["fileMd5"] = await asyncio.to_thread(_md5_file, path)
        return data

    async def voice_to_text(
        self,
        msg_server_id: int,
        *,
        seq: int = 0,
        timeout: float = 15.0,
        interval: float = 0.8,
    ) -> str:
        """``/msg/voiceToTextApply`` + ``/msg/voiceToTextQuery``：语音转文字。

        ``msg_server_id`` 必须是企业微信消息 ID（``structured.msgServerId``），
        不是本地事件游标 id。任务未完成时按 ``interval`` 轮询，超时抛错。
        """
        msg_id = int(msg_server_id or 0)
        if msg_id <= 0:
            raise QWPadApiError("语音转文字缺少 msgServerId", retryable=False)
        applied = await self.post("/msg/voiceToTextApply", {"msgServerId": msg_id})
        voice_id = str((applied or {}).get("voiceId") or "").strip()
        if not voice_id:
            raise QWPadApiError("语音转文字申请未返回 voiceId", retryable=False)
        payload: dict[str, Any] = {"msgServerId": msg_id, "voiceId": voice_id}
        if seq:
            payload["seq"] = int(seq)
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
        while True:
            data = await self.post("/msg/voiceToTextQuery", payload) or {}
            text = str(data.get("text") or "").strip()
            if data.get("isEnd"):
                return text
            if asyncio.get_running_loop().time() >= deadline:
                raise QWPadApiError("语音转文字超时（任务未完成）", retryable=True)
            await asyncio.sleep(max(0.2, interval))

    async def upload_file(
        self, path: str, file_type: int = FILE_TYPE_FILE
    ) -> dict[str, Any]:
        """``POST /cloud/cdnBigUpload``（multipart）→ 上传凭证。

        普通 CDN 要求非空且 **小于 20MiB**；更大的视频/文件请用
        :meth:`upload_big_file`（``/cloud/cdnBigFileUpload``）。
        文件以句柄流式提交，避免把大文件整体读入内存。
        """
        size = self._check_upload_size(path, file_type)
        name = os.path.basename(path) or "file"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        async with _open_binary(path) as fh:
            data = await self._request(
                "POST",
                "/cloud/cdnBigUpload",
                files={"file": (name, fh, mime)},
                data={"fileType": str(int(file_type))},
                timeout=UPLOAD_TIMEOUT,
            )
        return await self._fill_upload_metadata(dict(data or {}), path, size)

    async def upload_big_file(
        self, path: str, file_type: int = BIG_FILE_TYPE_FILE
    ) -> dict[str, Any]:
        """``POST /cloud/cdnBigFileUpload``（multipart）→ BigCDN 凭证。

        ``file_type``：22 视频 / 35 文件。返回值中的 ``fileConvertId`` 是发送
        ``/msg/sendBigFile|sendBigVideo`` 所需的 ``*1*`` 文件 ID。
        """
        if file_type not in (BIG_FILE_TYPE_FILE, BIG_FILE_TYPE_VIDEO):
            raise QWPadApiError(
                f"大文件上传 fileType 只能是 35（文件）或 22（视频），收到 {file_type}",
                retryable=False,
            )
        if not os.path.isfile(path):
            raise QWPadApiError(f"上传失败：文件不存在 {path}", retryable=False)
        size = os.path.getsize(path)
        if size <= 0:
            raise QWPadApiError(
                f"上传失败：文件为空 {os.path.basename(path)}", retryable=False
            )
        name = os.path.basename(path) or "file"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        async with _open_binary(path) as fh:
            data = await self._request(
                "POST",
                "/cloud/cdnBigFileUpload",
                files={"file": (name, fh, mime)},
                data={"fileType": str(int(file_type))},
                timeout=BIG_UPLOAD_TIMEOUT,
            )
        data = dict(data or {})
        if not str(data.get("fileConvertId") or "").strip():
            raise QWPadApiError(
                "大文件上传成功但未返回 fileConvertId，无法发送", retryable=False
            )
        return await self._fill_upload_metadata(data, path, size)

    async def download_wxwork_file(self, ref: dict[str, Any]) -> bytes | None:
        """``/cloud/wxWorkDownload`` 拿 cloudUrl，再把字节取回。"""
        file_id = ref.get("fileId")
        if not file_id:
            return None
        payload = {
            "fileId": file_id,
            "fileAeskey": ref.get("aesKey")
            or ref.get("fileAeskey")
            or ref.get("fileAesKey")
            or "",
            "fileSize": int(ref.get("size") or ref.get("fileSize") or 0),
            "fileType": int(ref.get("fileType") or FILE_TYPE_FILE),
        }
        data = await self.post("/cloud/wxWorkDownload", payload) or {}
        url = data.get("cloudUrl")
        if not url:
            return None
        return await self._download_url(url)

    async def download_wx_file(self, ref: dict[str, Any]) -> bytes | None:
        """``/cloud/wxDownload`` 下载个人微信文件（入站 msgType 101 等）。

        ``/cloud/wxDownload`` 请求里要的是直接下载地址（big/middle/thumb），
        不是 fileId。实测原图/中图可能 502，只有缩略图可用，所以按候选顺序回退。
        """
        candidates = list(ref.get("wxCandidates") or [])
        if not candidates:
            return None
        aes_key = str(ref.get("aesKey") or ref.get("fileAesKey") or "")
        auth_key = str(ref.get("fileAuthKey") or ref.get("fileAuthkey") or "")
        failures: list[str] = []
        for cand in candidates:
            url = str((cand or {}).get("fileUrl") or "")
            if not url:
                continue
            file_type = int((cand or {}).get("fileType") or 0)
            payload = {
                "fileUrl": url,
                "fileAuthkey": auth_key,
                "fileAeskey": aes_key,
                "fileSize": int((cand or {}).get("fileSize") or 0),
                "fileType": file_type,
            }
            try:
                data = (
                    await self.post("/cloud/wxDownload", payload, timeout=120.0) or {}
                )
            except QWPadError as exc:
                failures.append(f"type={file_type}: {exc}")
                continue
            cloud_url = data.get("cloudUrl")
            if not cloud_url:
                failures.append(f"type={file_type}: 无 cloudUrl")
                continue
            try:
                raw = await self._download_url(cloud_url)
            except QWPadError as exc:
                failures.append(f"type={file_type}: {exc}")
                continue
            if raw:
                if file_type != 1:
                    logger.info(
                        "[QWPad] 原图不可用，已退回 type=%s 尺寸（%s 字节）",
                        file_type,
                        len(raw),
                    )
                return raw
        logger.warning(
            "[QWPad] /cloud/wxDownload 全部尺寸失败: %s", "; ".join(failures)
        )
        return None

    async def _download_url(
        self, url: str, max_bytes: int = MAX_DOWNLOAD_BYTES
    ) -> bytes:
        parts = urlsplit(str(url or ""))
        if parts.scheme not in ("http", "https"):
            raise QWPadApiError(
                "下载地址不是 HTTP(S)（"
                f"{parts.scheme or '无协议'}）；QWPad 存储未开启 HTTP 出口时"
                "会返回 fs:// 对象引用，需部署方启用本地下载出口",
                retryable=False,
            )
        # 独立无鉴权客户端：cloudUrl 属于对象存储，不能收到 Bearer TOKEN。
        http = await self._ensure_download_http()
        try:
            async with http.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise QWPadApiError(
                            f"下载超过上限 {max_bytes} 字节", retryable=False
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise QWPadError(
                f"下载失败 {url.split('?')[0]}: {exc}", retryable=True
            ) from exc

    # ── 群 / 联系人 ─────────────────────────────────────────────────────────
    async def get_room_detail(self, room_id: str) -> dict[str, Any]:
        return await self.post("/room/getRoomDetail", {"roomId": str(room_id)}) or {}

    async def get_room_list(
        self, limit: int = 200, current_seq: int = 0
    ) -> dict[str, Any]:
        return (
            await self.post(
                "/room/getRoomList",
                {"limit": int(limit), "currentSeq": int(current_seq)},
            )
            or {}
        )

    async def batch_get_userinfo(self, user_ids: list[str]) -> dict[str, Any]:
        return (
            await self.post(
                "/contact/batchGetUserinfo",
                {"userIdList": [str(u) for u in user_ids]},
            )
            or {}
        )

    async def search_contact(self, keyword: str) -> dict[str, Any]:
        return await self.post("/contact/searchContact", {"keyword": keyword}) or {}


__all__ = [
    "BIG_FILE_TYPE_FILE",
    "BIG_FILE_TYPE_VIDEO",
    "BROWSER_UA",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "FILE_TYPE_FILE",
    "FILE_TYPE_IMAGE",
    "FILE_TYPE_VIDEO",
    "MAX_CDN_UPLOAD_BYTES",
    "MSG_TYPE_NAMES",
    "EventsPage",
    "QWPadApiError",
    "QWPadAuthError",
    "QWPadClient",
    "QWPadError",
]
