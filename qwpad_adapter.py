"""QWPad（企业微信 pad 协议）AstrBot 平台适配器。

一个 TOKEN = 一个企业微信账号 = 一个平台实例。多账号请在 AstrBot 里添加多个
QWPad 平台实例（``id`` 唯一，各自一个 TOKEN），每个实例独立轮询/游标/客户端/
策略/统计；同一 TOKEN 不允许被两个实例同时使用。

收消息：``GET /events`` long-poll（unified），游标持久化，重启不重放。
发消息：``/msg/sendText|sendHyperText|sendImage|sendFile|sendVoice|sendVideo``。

配置键见 ``QWPad_CONFIG_METADATA``。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.api.platform import (
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .qwpad_client import (
    DEFAULT_BASE_URL,
    QWPadAuthError,
    QWPadClient,
    QWPadError,
)
from .qwpad_event import QWPadMessageEvent
from .qwpad_media import cache_media
from .qwpad_messages import InboundMessage, parse_event, reply_context, strip_html
from .qwpad_policy import PolicySnapshot, as_bool, as_int, classify
from .qwpad_send import MAX_MESSAGE_LENGTH, send_chain
from .qwpad_storage import (
    CursorStore,
    DedupeSet,
    instance_dir,
    token_fingerprint,
)

PLATFORM_NAME = "wecom(Pad)"
PLATFORM_DESCRIPTION = "QWPad 企业微信 pad 协议适配器"
STATUS_CHECK_INTERVAL_S = 300.0
NAME_CACHE_TTL_S = 900.0
# 自动引用窗口：入站消息后这么久内的首条发送引用它；超时视为普通通知，不引用。
QUOTE_WINDOW_S = 600.0
MAX_FORWARD_REFS = 10
# openapi：/user/getLoginStatus 的 userOnlineStatus=2 才表示当前 PAD 会话在线；
# errorCode=0 只代表“查询成功”，账号掉线时也可能返回 0。
ONLINE_STATUS_VALUE = 2


def account_online_state(status: dict[str, Any] | None) -> bool | None:
    """True=在线，False=不在线，None=响应没带该字段（无法判断）。"""
    if not isinstance(status, dict) or "userOnlineStatus" not in status:
        return None
    return as_int(status.get("userOnlineStatus"), -1) == ONLINE_STATUS_VALUE


QWPad_CONFIG_METADATA: dict[str, dict[str, Any]] = {
    "token": {
        "description": "QWPad 业务 TOKEN（wxa_ 前缀）",
        "type": "string",
        "secret": True,
        "hint": "一个 TOKEN 对应一个企业微信账号。多账号请再添加一个「wecom(Pad)」平台实例，id 唯一、各自填 TOKEN。",
    },
    "api_base": {
        "description": "QWPad API 地址",
        "type": "string",
        "hint": "默认 https://api.qwpad.com，私有化部署时改这里。",
    },
    "allow_users": {
        "description": "私聊/群成员白名单（成员 userId，逗号分隔）",
        "type": "string",
        "hint": "留空且未开启 allow_all_users 时，所有私聊都会被拒绝。群消息里成员在白名单也可放行。",
    },
    "allow_groups": {
        "description": "群白名单（roomId，逗号分隔）",
        "type": "string",
        "hint": "只有名单内群的消息会进入 AstrBot（除非 allow_all_users=true）。",
    },
    "block_users": {
        "description": "用户黑名单（userId，逗号分隔）",
        "type": "string",
        "hint": "黑名单优先于白名单，私聊和群聊都生效。",
    },
    "block_groups": {
        "description": "群黑名单（roomId，逗号分隔）",
        "type": "string",
        "hint": "黑名单优先于白名单。",
    },
    "allow_all_users": {
        "description": "放开所有聊天（仅调试/内部账号）",
        "type": "bool",
        "hint": "默认 false。开启后仍受 block_users / block_groups 约束，且非聊天事件依旧丢弃。",
    },
    "group_reply": {
        "description": "群消息策略",
        "type": "string",
        "hint": "atbot=只放行 @ 我的群消息（默认）；all=放行全部群消息（未 @ 只进事件流，不强制唤醒）；none=忽略所有群消息。",
    },
    "quote_replies": {
        "description": "回复时引用原消息",
        "type": "bool",
        "hint": "默认 true。只在原消息是文本/富文本时引用；引用发送失败会自动退化为普通文本。",
    },
    "strip_markdown": {
        "description": "发送前清洗 Markdown",
        "type": "bool",
        "hint": "默认 true。企业微信不渲染 Markdown，清洗后更易读。",
    },
    "poll_wait_seconds": {
        "description": "long-poll 等待秒数",
        "type": "int",
        "hint": "1-30，默认 25。越小越实时但请求越多。",
    },
    "poll_limit": {
        "description": "单次拉取事件条数",
        "type": "int",
        "hint": "1-500，默认 50。",
    },
    "max_message_length": {
        "description": "单条文本上限（字符）",
        "type": "int",
        "hint": "默认 2000。超长自动分片发送。",
    },
    "startup_backlog_minutes": {
        "description": "首次启动回看窗口（分钟）",
        "type": "int",
        "hint": "默认 5：没有游标时只处理最近 5 分钟内的消息，避免重启后回复旧消息。0=处理服务端全部积压。",
    },
}


@register_platform_adapter(
    PLATFORM_NAME,
    PLATFORM_DESCRIPTION,
    adapter_display_name="wecom(Pad)",
    logo_path="logo.png",
    default_config_tmpl={
        "token": "",
        "api_base": DEFAULT_BASE_URL,
        "allow_users": "",
        "allow_groups": "",
        "block_users": "",
        "block_groups": "",
        "allow_all_users": False,
        "group_reply": "atbot",
        "quote_replies": True,
        "strip_markdown": True,
        "poll_wait_seconds": 25,
        "poll_limit": 50,
        "max_message_length": MAX_MESSAGE_LENGTH,
        "startup_backlog_minutes": 5,
    },
    config_metadata=QWPad_CONFIG_METADATA,
    support_streaming_message=False,
)
class QWPadAdapter(Platform):
    """QWPad 适配器：一个平台实例对应一个 TOKEN / 一个企业微信账号。"""

    # 进程内 token 占用表：同一 TOKEN 只能有一个实例轮询（否则游标/消息会重复）。
    _ACTIVE_TOKENS: dict[str, str] = {}
    # 供插件 Web API 查询的实例表（按平台实例 id）。
    instances: dict[str, "QWPadAdapter"] = {}

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.config = platform_config
        self.settings = platform_settings or {}
        self.instance_id = str(platform_config.get("id") or PLATFORM_NAME)

        self.token = str(platform_config.get("token") or "").strip()
        self.api_base = str(
            platform_config.get("api_base") or DEFAULT_BASE_URL
        ).strip().rstrip("/")
        self.policy = PolicySnapshot.from_config(platform_config)
        self.quote_replies = as_bool(platform_config.get("quote_replies"), True)
        self.strip_markdown = as_bool(platform_config.get("strip_markdown"), True)
        self.poll_wait_seconds = max(
            1, min(as_int(platform_config.get("poll_wait_seconds"), 25), 30)
        )
        self.poll_limit = max(1, min(as_int(platform_config.get("poll_limit"), 50), 500))
        self.max_message_length = max(
            200, as_int(platform_config.get("max_message_length"), MAX_MESSAGE_LENGTH)
        )
        self.startup_backlog_minutes = max(
            0, as_int(platform_config.get("startup_backlog_minutes"), 5)
        )

        self.metadata = PlatformMetadata(
            name=PLATFORM_NAME,
            description=PLATFORM_DESCRIPTION,
            id=self.instance_id,
            adapter_display_name="wecom(Pad)",
            support_streaming_message=False,
            support_proactive_message=True,
            module_path=__name__,
            config_metadata=QWPad_CONFIG_METADATA,
        )

        self._client: QWPadClient | None = None
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self._cursor_store: CursorStore | None = None
        self._cursor = 0
        self._dedupe = DedupeSet()
        self._self_id = ""
        self._self_nick = ""
        self._corp_name = ""
        self._account_online: bool | None = None
        self._last_status_check = 0.0
        self._skip_before = 0.0
        self._room_cache: dict[str, tuple[str, float]] = {}
        self._contact_cache: dict[str, tuple[str, float]] = {}
        self._replies: dict[str, dict[str, Any]] = {}
        self._pending_reply: dict[str, tuple[float, dict[str, Any]]] = {}
        # 注意：不要覆盖 Platform._started_at（父类语义是 datetime），
        # 平台状态置 RUNNING 时由父类 setter 自动写入。
        self._last_event_at = 0.0
        self._messages_in = 0
        self._messages_out = 0

    # ── AstrBot 契约 ───────────────────────────────────────────────────────
    def meta(self) -> PlatformMetadata:
        return self.metadata

    def create_event(self, message: AstrBotMessage) -> QWPadMessageEvent:
        session_id = str(getattr(message, "session_id", "") or "")
        anchor = self._pending_reply.pop(session_id, None)
        reply_anchor = None
        if anchor:
            remembered_at, data = anchor
            fresh = time.monotonic() - remembered_at <= QUOTE_WINDOW_S
            if fresh and self._anchor_is_quotable(data):
                reply_anchor = data
        return QWPadMessageEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=session_id,
            client=self._client,
            adapter=self,
            chat_id=session_id,
            reply_anchor=reply_anchor,
        )

    def get_client(self) -> QWPadClient | None:
        return self._client

    def record_outbound(self, count: int) -> None:
        """由事件层上报实际发出的消息条数（event.send 不经过 send_by_session）。"""
        self._messages_out += max(0, int(count))

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats.update(
            {
                "connected": self._running,
                "account": {
                    "user_id": self._self_id,
                    "nickname": self._self_nick,
                    "corp_name": self._corp_name,
                    "online": self._account_online,
                },
                "cursor": self._cursor,
                "dedupe_size": len(self._dedupe),
                "messages_in": self._messages_in,
                "messages_out": self._messages_out,
                "last_event_at": self._last_event_at,
                "policy": {
                    "allow_users": sorted(self.policy.allow_users),
                    "allow_groups": sorted(self.policy.allow_groups),
                    "block_users": sorted(self.policy.block_users),
                    "block_groups": sorted(self.policy.block_groups),
                    "allow_all_users": self.policy.allow_all_users,
                    "group_reply": self.policy.group_reply,
                },
            }
        )
        return stats

    # ── 生命周期 ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        if not self.token:
            raise ValueError("QWPad: TOKEN 未配置（平台配置里的 token 字段）")

        fingerprint = token_fingerprint(self.token)
        active = self._ACTIVE_TOKENS.get(fingerprint)
        if active and active != self.instance_id:
            raise RuntimeError(
                f"QWPad: 同一 TOKEN 已被平台实例 {active} 使用；"
                "一个 TOKEN 只能运行一个实例，请禁用重复实例。"
            )
        self._ACTIVE_TOKENS[fingerprint] = self.instance_id
        self.instances[self.instance_id] = self

        self._client = QWPadClient(self.token, self.api_base)
        self._stop_event.clear()
        try:
            status = await self._client.get_login_status()
            self._self_id = str(status.get("userId") or "").strip()
            self._self_nick = str(status.get("nickname") or "").strip()
            self._corp_name = str(status.get("corpName") or "").strip()
            error_code = as_int(status.get("errorCode"), 0)
            if error_code != 0:
                raise RuntimeError(
                    "QWPad 登录状态查询失败（errorCode=%s %s），"
                    "请先在 QWPad 控制台检查账号与 TOKEN"
                    % (error_code, status.get("errorMsg") or "")
                )
            self._account_online = account_online_state(status)
            if self._account_online is False:
                raise RuntimeError(
                    "QWPad 账号未在线（userOnlineStatus=%s，errorCode=0 仅表示查询成功），"
                    "请先在 QWPad 控制台完成扫码登录"
                    % status.get("userOnlineStatus")
                )
            if self._account_online is None:
                logger.warning(
                    "[QWPad:%s] /user/getLoginStatus 未返回 userOnlineStatus，无法确认账号是否在线",
                    self.instance_id,
                )

            self._cursor_store = CursorStore(instance_dir(self.instance_id))
            self._cursor = self._cursor_store.load()
            if self._cursor == 0 and self.startup_backlog_minutes > 0:
                self._skip_before = time.time() - self.startup_backlog_minutes * 60
            if not self.policy.permits_anyone:
                logger.warning(
                    "[QWPad:%s] 白名单为空且 allow_all_users=false —— 所有消息都会被拒绝。"
                    "请配置 allow_users / allow_groups，或打开 allow_all_users。",
                    self.instance_id,
                )

            self._running = True
            self._last_status_check = time.monotonic()
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name=f"qwpad-{self.instance_id}-poll"
            )
            logger.info(
                "[QWPad:%s] 已连接 %s（%s / %s），游标 %s，策略 %s",
                self.instance_id,
                self._self_nick or self._self_id,
                self._corp_name,
                self._self_id,
                self._cursor,
                self.policy,
            )
            await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        except QWPadAuthError as exc:
            self.record_error(f"TOKEN 无效或已失效：{exc}")
            logger.error("[QWPad:%s] TOKEN 无效或已失效: %s", self.instance_id, exc)
            raise
        except Exception as exc:
            self.record_error(str(exc))
            logger.error("[QWPad:%s] 启动失败: %s", self.instance_id, exc)
            raise
        finally:
            await self._cleanup()
            self._ACTIVE_TOKENS.pop(fingerprint, None)
            self.instances.pop(self.instance_id, None)

    async def terminate(self) -> None:
        self._running = False
        self._stop_event.set()
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _cleanup(self) -> None:
        self._running = False
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    # ── 收消息 ──────────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        backoff = 1.0
        while self._running:
            client = self._client
            if client is None:
                return
            try:
                page = await client.fetch_events(
                    self._cursor,
                    limit=self.poll_limit,
                    wait_seconds=self.poll_wait_seconds,
                )
                for item in page.items:
                    await self._handle_event_item(item)
                    event_id = as_int(item.get("id"), 0)
                    if event_id > self._cursor:
                        self._cursor = event_id
                        if self._cursor_store is not None:
                            self._cursor_store.save(self._cursor)
                if page.next_id > self._cursor:
                    self._cursor = page.next_id
                    if self._cursor_store is not None:
                        self._cursor_store.save(self._cursor)
                backoff = 1.0
                if (
                    time.monotonic() - self._last_status_check
                    >= STATUS_CHECK_INTERVAL_S
                ):
                    await self._check_account_status()
            except asyncio.CancelledError:
                raise
            except QWPadAuthError as exc:
                self._running = False
                self.record_error(f"TOKEN 失效：{exc}")
                logger.error("[QWPad:%s] TOKEN 失效，停止轮询: %s", self.instance_id, exc)
                self._stop_event.set()
                return
            except QWPadError as exc:
                level = logger.warning if exc.retryable else logger.error
                level(
                    "[QWPad:%s] 拉取事件失败，%.0fs 后重试: %s",
                    self.instance_id,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as exc:  # noqa: BLE001 - 轮询循环不能死
                logger.warning(
                    "[QWPad:%s] 轮询异常，%.0fs 后重试: %s",
                    self.instance_id,
                    backoff,
                    exc,
                    exc_info=True,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _check_account_status(self) -> None:
        self._last_status_check = time.monotonic()
        client = self._client
        if client is None:
            return
        with contextlib.suppress(QWPadError):
            status = await client.get_login_status()
            self._account_online = account_online_state(status)
            if as_int(status.get("errorCode"), 0) != 0:
                logger.warning(
                    "[QWPad:%s] 账号状态查询失败: %s",
                    self.instance_id,
                    status.get("errorMsg") or status.get("errorCode"),
                )
            elif self._account_online is False:
                logger.warning(
                    "[QWPad:%s] 账号掉线（userOnlineStatus=%s），请重新扫码登录",
                    self.instance_id,
                    status.get("userOnlineStatus"),
                )

    async def _handle_event_item(self, item: dict[str, Any]) -> None:
        inbox = parse_event(item, self_id=self._self_id, self_nick=self._self_nick)
        if inbox is None or inbox.is_self:
            return
        if inbox.message_id in self._dedupe:
            return
        if self._skip_before and inbox.timestamp and inbox.timestamp < self._skip_before:
            self._dedupe.add(inbox.message_id)
            logger.debug(
                "[QWPad:%s] 首次启动跳过旧消息 id=%s ts=%s",
                self.instance_id,
                inbox.message_id,
                inbox.timestamp,
            )
            return

        decision = classify(self.policy, inbox)
        if not decision.allowed:
            self._dedupe.add(inbox.message_id)
            logger.debug(
                "[QWPad:%s] 策略拒绝 %s: room=%s user=%s",
                self.instance_id,
                decision.reason,
                inbox.chat_id,
                inbox.user_id,
            )
            return

        # 分发成功后才记入去重：分发抛错时不推进游标，下一轮还能重试；
        # 若提前去重，重试会被当成重复消息直接丢弃。
        await self._dispatch(inbox)
        self._dedupe.add(inbox.message_id)

    async def _dispatch(self, inbox: InboundMessage) -> None:
        refs = ([inbox.media] if inbox.media else []) + list(
            inbox.media_refs[:MAX_FORWARD_REFS]
        )
        downloaded: list[tuple[Any, str, str]] = []
        client = self._client
        if client is not None and refs:
            tasks = [
                cache_media(client, ref)
                for ref in refs
                if ref is not None and ref.downloadable
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            index = 0
            for ref in refs:
                if ref is None or not ref.downloadable:
                    continue
                result = results[index]
                index += 1
                if isinstance(result, BaseException):
                    logger.warning(
                        "[QWPad:%s] 下载媒体异常: %s", self.instance_id, result
                    )
                    continue
                if result:
                    path, kind = result
                    downloaded.append((ref, path, kind))

        chat_name = await self._chat_name(inbox)
        message = self._build_message(inbox, chat_name, downloaded)
        if self.quote_replies:
            self._remember_reply_anchor(inbox)

        event = self.create_event(message)
        for _, path, _ in downloaded:
            event.track_temporary_local_file(path)
        self._messages_in += 1
        self._last_event_at = time.time()
        logger.info(
            "[QWPad:%s] 收到%s消息 room=%s user=%s type=%s len=%s media=%s",
            self.instance_id,
            "群" if inbox.chat_type == "group" else "私",
            inbox.chat_id,
            inbox.user_name or inbox.user_id,
            inbox.msg_name,
            len(inbox.text or ""),
            len(downloaded),
        )
        self.commit_event(event)

    def _build_message(
        self,
        inbox: InboundMessage,
        chat_name: str,
        downloaded: list[tuple[Any, str, str]],
    ) -> AstrBotMessage:
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE
            if inbox.chat_type == "group"
            else MessageType.FRIEND_MESSAGE
        )
        message.self_id = self._self_id
        message.session_id = inbox.chat_id
        message.message_id = inbox.message_id
        message.sender = MessageMember(
            user_id=inbox.user_id,
            nickname=inbox.user_name or inbox.user_id,
        )
        message.raw_message = inbox.raw
        message.timestamp = inbox.timestamp or int(time.time())
        if inbox.chat_type == "group":
            message.group = Group(group_id=inbox.chat_id, group_name=chat_name)

        chain: list[Any] = []
        # 群聊唤醒依赖 At.qq == self_id；没有它，核心不会把群消息判给机器人。
        if inbox.chat_type == "group" and inbox.mentions_me and self._self_id:
            chain.append(At(qq=self._self_id, name=self._self_nick or ""))
        quoted = reply_context(inbox)
        if quoted:
            chain.append(
                Reply(
                    id=inbox.message_id,
                    sender_id=inbox.user_id,
                    sender_nickname=inbox.user_name,
                    message_str=quoted,
                    time=inbox.timestamp,
                )
            )

        has_media = bool(downloaded)
        text = inbox.text or ""
        if text and not (has_media and text == inbox.placeholder):
            chain.append(Plain(text))

        for ref, path, kind in downloaded:
            if kind == "image":
                chain.append(Image.fromFileSystem(path))
            elif kind == "voice":
                chain.append(Record.fromFileSystem(path))
            elif kind == "video":
                chain.append(Video.fromFileSystem(path))
            else:
                name = (
                    ref.filename
                    or ("voice.amr" if ref.kind == "voice" else "file")
                )
                chain.append(File(name=name, file=path))

        if not chain:
            chain.append(Plain(text or "[消息]"))
        message.message = chain
        message.message_str = text or inbox.placeholder or "[消息]"
        return message

    def _remember_reply_anchor(self, inbox: InboundMessage) -> None:
        """存下引用一条消息所需的字段（企业微信 reply 需要原消息结构）。"""
        structured = ((inbox.raw or {}).get("message") or {}).get("structured") or {}
        data = structured.get("msgData")
        original_text = data.get("content") if isinstance(data, dict) else ""
        if isinstance(original_text, str):
            original_text = strip_html(original_text)
        if not isinstance(original_text, str) or not original_text.strip():
            original_text = inbox.text or ""
        anchor = {
            "msgUniqueIdentifier": inbox.message_id,
            "showName": inbox.user_name or inbox.user_id,
            "timeStamp": inbox.timestamp,
            "type": inbox.msg_type,
            "userId": inbox.user_id,
            "msgData": data if isinstance(data, dict) else {"content": inbox.text or ""},
            "text": original_text,
        }
        while len(self._replies) > 500:
            self._replies.pop(next(iter(self._replies)), None)
        self._replies[inbox.message_id] = anchor
        self._pending_reply[inbox.chat_id] = (time.monotonic(), anchor)
        while len(self._pending_reply) > 200:
            self._pending_reply.pop(next(iter(self._pending_reply)), None)

    @staticmethod
    def _anchor_is_quotable(anchor: dict[str, Any]) -> bool:
        # 非文本原消息的引用渲染尚未验证；先退化成普通发送，避免异常气泡。
        return bool(anchor.get("msgUniqueIdentifier")) and anchor.get("type") in (0, 2)

    async def _chat_name(self, inbox: InboundMessage) -> str:
        if inbox.chat_type != "group":
            if inbox.user_name and inbox.user_name != inbox.user_id:
                return inbox.user_name
            cached = self._contact_cache.get(inbox.chat_id)
            if cached and time.monotonic() - cached[1] < NAME_CACHE_TTL_S:
                return cached[0]
            client = self._client
            if client is not None:
                try:
                    data = await asyncio.wait_for(
                        client.batch_get_userinfo([inbox.chat_id]), timeout=5.0
                    )
                    for entry in data.get("contactList") or []:
                        name = _contact_display_name(entry)
                        if name:
                            self._contact_cache[inbox.chat_id] = (
                                name,
                                time.monotonic(),
                            )
                            return name
                except Exception:  # noqa: BLE001 - 昵称只是装饰
                    logger.debug(
                        "[QWPad:%s] 私聊昵称查询失败 user=%s",
                        self.instance_id,
                        inbox.chat_id,
                        exc_info=True,
                    )
            return inbox.user_name or inbox.chat_id

        cached = self._room_cache.get(inbox.chat_id)
        if cached and time.monotonic() - cached[1] < NAME_CACHE_TTL_S:
            return cached[0]
        client = self._client
        if client is None:
            return cached[0] if cached else inbox.chat_id
        try:
            detail = await asyncio.wait_for(
                client.get_room_detail(inbox.chat_id), timeout=10.0
            )
        except Exception:  # noqa: BLE001 - 群名只是装饰
            return cached[0] if cached else inbox.chat_id
        name = (
            str(detail.get("roomName") or detail.get("name") or "").strip()
            or inbox.chat_id
        )
        self._room_cache[inbox.chat_id] = (name, time.monotonic())
        return name

    # ── 发消息 ──────────────────────────────────────────────────────────────
    async def send_by_session(
        self, session: MessageSession, message_chain: MessageChain
    ) -> None:
        client = self._client
        if client is None:
            logger.warning(
                "[QWPad:%s] 未连接，无法向会话 %s 发送",
                self.instance_id,
                session.session_id,
            )
            await super().send_by_session(session, message_chain)
            return
        try:
            stats = await send_chain(
                client,
                str(session.session_id),
                list(message_chain.chain),
                limit=self.max_message_length,
                strip_md=self.strip_markdown,
            )
            if stats.get("sent"):
                self._messages_out += int(stats["sent"])
            if stats.get("failed"):
                logger.warning(
                    "[QWPad:%s] send_by_session 部分失败: %s",
                    self.instance_id,
                    stats.get("errors"),
                )
        finally:
            await super().send_by_session(session, message_chain)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """群返回群名/人数；私聊返回联系人昵称。"""
        chat_id = str(chat_id)
        client = self._client
        if client is None:
            return {"name": chat_id, "type": "dm", "chat_id": chat_id}
        cached = self._room_cache.get(chat_id)
        if cached and time.monotonic() - cached[1] < NAME_CACHE_TTL_S:
            return {"name": cached[0], "type": "group", "chat_id": chat_id}
        try:
            detail = await asyncio.wait_for(
                client.get_room_detail(chat_id), timeout=10.0
            )
        except Exception:  # noqa: BLE001
            detail = {}
        if detail.get("roomName") or detail.get("name"):
            name = str(detail.get("roomName") or detail.get("name"))
            self._room_cache[chat_id] = (name, time.monotonic())
            return {
                "name": name,
                "type": "group",
                "chat_id": chat_id,
                "member_count": len(detail.get("memberList") or []),
            }
        contact = self._contact_cache.get(chat_id)
        if contact and time.monotonic() - contact[1] < NAME_CACHE_TTL_S:
            return {"name": contact[0], "type": "dm", "chat_id": chat_id}
        with contextlib.suppress(Exception):
            data = await asyncio.wait_for(
                client.batch_get_userinfo([chat_id]), timeout=10.0
            )
            for entry in data.get("contactList") or []:
                name = _contact_display_name(entry)
                if name:
                    self._contact_cache[chat_id] = (name, time.monotonic())
                    return {"name": name, "type": "dm", "chat_id": chat_id}
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}


def _contact_display_name(entry: dict[str, Any]) -> str:
    """从 ``batchGetUserinfo`` 条目里取可读名字（不同联系人字段不同）。"""
    full = entry.get("fullUserInfo")
    full = full if isinstance(full, dict) else {}
    for value in (
        entry.get("nickname"),
        entry.get("realName"),
        entry.get("name"),
        full.get("name"),
        entry.get("acctid"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


__all__ = ["PLATFORM_NAME", "QWPadAdapter"]
