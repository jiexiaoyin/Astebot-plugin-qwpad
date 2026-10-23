import asyncio
from types import SimpleNamespace

import pytest
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Location, Plain, Record, Reply
from astrbot.core.platform.platform import PlatformStatus
from qwpad_plugin import qwpad_adapter
from qwpad_plugin.qwpad_adapter import (
    QUOTE_WINDOW_S,
    QWPadAdapter,
    account_online_state,
)
from qwpad_plugin.qwpad_client import QWPadApiError, QWPadAuthError, QWPadError
from qwpad_plugin.qwpad_messages import parse_event
from qwpad_plugin.qwpad_policy import PolicySnapshot
from qwpad_plugin.qwpad_send import _record_path, send_chain
from qwpad_plugin.qwpad_storage import CursorStore


def _adapter():
    adapter = QWPadAdapter(
        {
            "id": "qwpad-test",
            "token": "wxa_test",
            "allow_all_users": True,
            "group_reply": "atbot",
            "quote_replies": True,
        },
        {},
        asyncio.Queue(),
    )
    adapter._self_id = "999"
    adapter._self_nick = "小助理"
    return adapter


def _group_item():
    return {
        "id": 11,
        "type": 15000,
        "display": {
            "category": "chat",
            "senderId": "1001",
            "roomId": "room-1",
            "senderName": "张三",
            "messageId": "disp-1",
        },
        "message": {
            "msgType": 0,
            "structured": {
                "senderId": "1001",
                "fromRoomId": "room-1",
                "senderName": "张三",
                "msgType": 0,
                "msgUniqueIdentifier": "uniq-11",
                "timestamp": 1792627702,
                "msgData": {
                    "content": "@小助理 库存还有吗",
                    "atList": [{"userId": "999"}],
                },
            },
        },
    }


def test_build_group_message_injects_at():
    adapter = _adapter()
    inbox = parse_event(_group_item(), self_id="999", self_nick="小助理")
    assert inbox is not None
    msg = adapter._build_message(inbox, "测试群", [])
    assert msg.self_id == "999"
    assert msg.session_id == "room-1"
    assert msg.group is not None and msg.group.group_name == "测试群"
    assert isinstance(msg.message[0], At)
    assert str(msg.message[0].qq) == "999"
    assert msg.message[1].text == "库存还有吗"
    assert msg.message_str == "库存还有吗"


def test_reply_anchor_and_event():
    adapter = _adapter()
    item = _group_item()
    item["message"]["structured"]["msgData"]["reply"] = {
        "msgData": {"content": "上一条"},
        "showName": "李四",
    }
    inbox = parse_event(item, self_id="999", self_nick="小助理")
    assert inbox is not None
    msg = adapter._build_message(inbox, "测试群", [])
    assert any(isinstance(c, Reply) for c in msg.message)

    adapter._remember_reply_anchor(inbox)
    event = adapter.create_event(msg)
    assert event._reply_anchor is not None
    assert event._reply_anchor["msgUniqueIdentifier"] == "uniq-11"
    # 锚点只消费一次
    assert adapter.create_event(msg)._reply_anchor is None


class FakeClient:
    def __init__(self):
        self.texts = []
        self.voice_calls = []

    async def voice_to_text(self, msg_server_id, *, seq=0):
        self.voice_calls.append((msg_server_id, seq))
        return "明天天气怎么样"

    async def get_room_detail(self, room_id):
        return {"roomName": "测试群"}

    async def batch_get_userinfo(self, user_ids):
        return {"contactList": []}

    async def send_text(self, to_id, content, reply=None):
        self.texts.append({"to": to_id, "content": content, "reply": reply})
        return {"msgUniqueIdentifier": "sent-1"}

    async def send_hyper_text(self, to_id, content, reply=None):
        self.texts.append({"to": to_id, "content": content, "reply": reply})
        return {"msgUniqueIdentifier": "sent-2"}


def test_handle_event_item_commits_to_event_queue():
    adapter = _adapter()
    adapter._client = FakeClient()
    adapter.policy = PolicySnapshot(allow_all_users=True)
    asyncio.run(adapter._handle_event_item(_group_item()))

    event = adapter._event_queue.get_nowait()
    assert event.message_obj.session_id == "room-1"
    assert event.message_obj.group.group_name == "测试群"
    assert adapter.get_stats()["messages_in"] == 1
    # 同一条消息再次进入会被去重
    asyncio.run(adapter._handle_event_item(_group_item()))
    assert adapter._event_queue.qsize() == 0


def _dm_item():
    item = _group_item()
    item["display"]["roomId"] = "0"
    item["display"]["senderName"] = "张三"
    item["message"]["structured"]["fromRoomId"] = "0"
    item["message"]["structured"]["msgData"] = {"content": "你好"}
    return item


def test_event_send_records_outbound_count(caplog):
    adapter = _adapter()
    client = FakeClient()
    adapter._client = client
    inbox = parse_event(_dm_item(), self_id="999", self_nick="小助理")
    event = adapter.create_event(adapter._build_message(inbox, "张三", []))

    asyncio.run(event.send(MessageChain([Plain("收到")])))

    assert client.texts[0]["content"] == "收到"
    assert adapter.get_stats()["messages_out"] == 1
    # 发送成功路径不能触发被吞掉的异常日志（曾因局部 import logger 触发
    # UnboundLocalError，测试仍绿但线上每次发送都报错）。
    assert "发送消息异常" not in caplog.text


class _FailClient(FakeClient):
    async def send_text(self, *a, **k):
        raise QWPadError("vendor down", retryable=False)

    async def send_hyper_text(self, *a, **k):
        raise QWPadError("vendor down", retryable=False)


def test_event_send_raises_when_all_failed():
    """全部发送失败必须抛给核心，不能静默当成已回复。"""
    adapter = _adapter()
    adapter._client = _FailClient()
    inbox = parse_event(_dm_item(), self_id="999", self_nick="小助理")
    event = adapter.create_event(adapter._build_message(inbox, "张三", []))
    with pytest.raises(RuntimeError, match="发送全部失败"):
        asyncio.run(event.send(MessageChain([Plain("收到")])))
    assert adapter.get_stats()["messages_out"] == 0


def test_send_by_session_raises_when_all_failed():
    adapter = _adapter()
    adapter._client = _FailClient()
    session = SimpleNamespace(session_id="1001")
    with pytest.raises(RuntimeError, match="发送全部失败"):
        asyncio.run(adapter.send_by_session(session, MessageChain([Plain("收到")])))
    assert adapter.get_stats()["messages_out"] == 0


def test_send_by_session_raises_when_not_connected():
    adapter = _adapter()
    adapter._client = None
    session = SimpleNamespace(session_id="1001")
    with pytest.raises(RuntimeError, match="未连接"):
        asyncio.run(adapter.send_by_session(session, MessageChain([Plain("收到")])))


class _NamedClient(FakeClient):
    def __init__(self, name):
        super().__init__()
        self.name = name


def _dm_item_for(user_id, text):
    item = _dm_item()
    item["display"]["senderId"] = user_id
    item["message"]["structured"]["senderId"] = user_id
    item["message"]["structured"]["msgData"] = {"content": text}
    return item


def test_voice_message_uses_qwpad_transcript():
    from qwpad_plugin.qwpad_client import MSG_VOICE_CDN

    adapter = _adapter()
    client = FakeClient()
    adapter._client = client
    item = _group_item()
    item["message"]["msgType"] = MSG_VOICE_CDN
    item["message"]["structured"]["msgType"] = MSG_VOICE_CDN
    item["message"]["structured"]["msgServerId"] = 987654321
    item["message"]["structured"]["seq"] = 5
    item["message"]["structured"]["msgData"] = {
        "voiceTime": 3,
        "atList": [{"userId": "999"}],
    }
    item["display"]["messageType"] = {"nativeName": "kVoiceCDN"}
    item["display"]["parseStatus"] = "parsed_media"

    inbox = parse_event(item, self_id="999", self_nick="小助理")
    assert inbox is not None and inbox.media is not None
    assert inbox.media.kind == "voice"
    asyncio.run(adapter._handle_event_item(item))

    event = adapter._event_queue.get_nowait()
    assert client.voice_calls == [(987654321, 5)]
    assert event.message_obj.message_str == "明天天气怎么样"
    assert any(
        isinstance(c, Plain) and c.text == "明天天气怎么样"
        for c in event.message_obj.message
    )


def test_transcribed_voice_omits_record_attachment(monkeypatch, tmp_path):
    """有转写文本时不再把 WAV 交给模型，避免模型去读二进制。"""
    from qwpad_plugin import qwpad_adapter
    from qwpad_plugin.qwpad_client import MSG_VOICE_CDN

    voice_file = tmp_path / "voice.wav"
    voice_file.write_bytes(b"RIFF")

    async def fake_cache(client, ref):
        return str(voice_file), "voice"

    monkeypatch.setattr(qwpad_adapter, "cache_media", fake_cache)
    adapter = _adapter()
    adapter._client = FakeClient()
    item = _group_item()
    item["message"]["msgType"] = MSG_VOICE_CDN
    item["message"]["structured"]["msgType"] = MSG_VOICE_CDN
    item["message"]["structured"]["msgServerId"] = 123
    item["message"]["structured"]["msgData"] = {
        "fileId": "voice-fid",
        "voiceTime": 3,
        "atList": [{"userId": "999"}],
    }
    item["display"]["messageType"] = {"nativeName": "kVoiceCDN"}
    item["display"]["parseStatus"] = "parsed_media"

    asyncio.run(adapter._handle_event_item(item))

    event = adapter._event_queue.get_nowait()
    assert not any(isinstance(c, Record) for c in event.message_obj.message)
    assert event.message_obj.message_str == "明天天气怎么样"
    assert not voice_file.exists()


def test_dispatch_failure_does_not_lose_message(monkeypatch):
    """分发抛错时不能提前去重，否则重试会被当成重复消息丢弃。"""
    adapter = _adapter()
    original = adapter._dispatch
    attempts = []

    async def flaky_dispatch(inbox):
        attempts.append(inbox.message_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        await original(inbox)

    monkeypatch.setattr(adapter, "_dispatch", flaky_dispatch)
    with pytest.raises(RuntimeError, match="temporary failure"):
        asyncio.run(adapter._handle_event_item(_group_item()))

    asyncio.run(adapter._handle_event_item(_group_item()))
    assert len(attempts) == 2
    assert adapter._event_queue.qsize() == 1
    assert adapter.get_stats()["messages_in"] == 1


def test_multiple_instances_are_isolated():
    """多账号 = 多个平台实例：客户端/游标/策略/统计互不串线。"""
    a = QWPadAdapter(
        {
            "id": "qwpad-a",
            "token": "wxa_account_a",
            "allow_all_users": True,
            "group_reply": "atbot",
        },
        {},
        asyncio.Queue(),
    )
    b = QWPadAdapter(
        {
            "id": "qwpad-b",
            "token": "wxa_account_b",
            "allow_all_users": True,
            "group_reply": "none",
        },
        {},
        asyncio.Queue(),
    )
    for adapter, self_id, nick in ((a, "9001", "助理A"), (b, "9002", "助理B")):
        adapter._self_id = self_id
        adapter._self_nick = nick
        adapter._client = _NamedClient(adapter.instance_id)
        adapter.policy = PolicySnapshot(
            allow_all_users=True, group_reply=adapter.config["group_reply"]
        )

    event_a = a.create_event(
        a._build_message(
            parse_event(_dm_item_for("1001", "给A"), self_id="9001", self_nick="助理A"),
            "张三",
            [],
        )
    )
    event_b = b.create_event(
        b._build_message(
            parse_event(_dm_item_for("1001", "给B"), self_id="9002", self_nick="助理B"),
            "张三",
            [],
        )
    )
    asyncio.run(event_a.send(MessageChain([Plain("A的回复")])))
    asyncio.run(event_b.send(MessageChain([Plain("B的回复")])))

    assert event_a.message_obj.self_id == "9001"
    assert event_b.message_obj.self_id == "9002"
    assert [t["content"] for t in a._client.texts] == ["A的回复"]
    assert [t["content"] for t in b._client.texts] == ["B的回复"]
    assert a.policy.group_reply == "atbot"
    assert b.policy.group_reply == "none"
    assert a.get_stats()["messages_out"] == 1
    assert b.get_stats()["messages_out"] == 1
    # 游标目录按平台实例隔离
    from qwpad_plugin.qwpad_storage import instance_dir, token_fingerprint

    assert instance_dir(a.instance_id) != instance_dir(b.instance_id)
    assert token_fingerprint(a.token) != token_fingerprint(b.token)


def test_same_token_cannot_run_twice():
    """同一 TOKEN 只能有一个平台实例在跑，否则会互抢游标/重复回复。"""
    a = QWPadAdapter({"id": "qwpad-a", "token": "wxa_same"}, {}, asyncio.Queue())
    b = QWPadAdapter({"id": "qwpad-b", "token": "wxa_same"}, {}, asyncio.Queue())
    from qwpad_plugin.qwpad_storage import token_fingerprint

    fp = token_fingerprint("wxa_same")
    QWPadAdapter._ACTIVE_TOKENS[fp] = a.instance_id
    try:
        import pytest

        with pytest.raises(RuntimeError, match="同一 TOKEN"):
            asyncio.run(b.run())
    finally:
        QWPadAdapter._ACTIVE_TOKENS.pop(fp, None)


def test_platform_metadata_name_is_wecom_pad():
    """AstrBot 4.29 的机器人列表/新增下拉直接显示适配器名，必须是 wecom(Pad)。"""
    meta = _adapter().meta()
    assert meta.name == "wecom(Pad)"
    assert meta.adapter_display_name == "wecom(Pad)"


def test_get_stats_follows_base_contract():
    adapter = _adapter()
    stats = adapter.get_stats()
    assert stats["id"] == "qwpad-test"
    # 父类 _started_at 是 datetime | None，聚合统计不能把它当成 float 覆盖。
    assert stats["started_at"] is None
    assert stats["connected"] is False
    assert stats["policy"]["allow_all_users"] is True


def test_account_online_state_uses_user_online_status():
    # openapi：errorCode=0 只表示查询成功；userOnlineStatus=2 才是在线。
    assert account_online_state({"errorCode": 0, "userOnlineStatus": 2}) is True
    assert account_online_state({"errorCode": 0, "userOnlineStatus": 0}) is False
    assert account_online_state({"errorCode": 0, "userOnlineStatus": 1}) is False
    assert account_online_state({"errorCode": 0}) is None
    assert account_online_state(None) is None


def test_run_rejects_offline_account(monkeypatch):
    from qwpad_plugin import qwpad_adapter

    class OfflineClient:
        async def get_login_status(self):
            return {
                "errorCode": 0,
                "errorMsg": "",
                "userId": "1001",
                "userOnlineStatus": 0,
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        qwpad_adapter, "QWPadClient", lambda *args, **kwargs: OfflineClient()
    )
    adapter = QWPadAdapter(
        {"id": "qwpad-offline", "token": "wxa_offline"}, {}, asyncio.Queue()
    )

    async def run():
        with pytest.raises(RuntimeError, match="userOnlineStatus=0"):
            await adapter.run()

    asyncio.run(run())


def test_check_account_status_warns_when_offline(caplog):
    class StatusClient:
        async def get_login_status(self):
            return {"errorCode": 0, "userOnlineStatus": 0, "errorMsg": ""}

    adapter = _adapter()
    adapter._client = StatusClient()
    asyncio.run(adapter._check_account_status())
    assert "账号掉线" in caplog.text
    assert adapter.get_stats()["account"]["online"] is False


def test_quote_anchor_expires_after_window():
    adapter = _adapter()
    inbox = parse_event(_dm_item(), self_id="999", self_nick="小助理")
    message = adapter._build_message(inbox, "张三", [])
    adapter._remember_reply_anchor(inbox)

    old_ts, data = adapter._pending_reply["1001"]
    adapter._pending_reply["1001"] = (old_ts - QUOTE_WINDOW_S - 1, data)
    assert adapter.create_event(message)._reply_anchor is None

    adapter._remember_reply_anchor(inbox)
    anchor = adapter.create_event(message)._reply_anchor
    assert anchor is not None


def test_non_text_anchor_not_quoted():
    adapter = _adapter()
    item = _group_item()
    item["message"]["msgType"] = 1
    item["message"]["structured"]["msgType"] = 1
    item["display"]["messageType"] = {"nativeName": "kImage"}
    item["display"]["parseStatus"] = "parsed_media"
    item["message"]["structured"]["msgData"] = {
        "fileId": "fid-1",
        "atList": [{"userId": "999"}],
    }
    inbox = parse_event(item, self_id="999", self_nick="小助理")
    assert inbox is not None
    adapter._remember_reply_anchor(inbox)
    event = adapter.create_event(adapter._build_message(inbox, "测试群", []))
    assert event._reply_anchor is None


def test_fatal_auth_error_propagates_and_marks_error(monkeypatch):
    """轮询里 TOKEN 失效时 run() 必须抛错，平台状态不能停在 RUNNING。"""
    from qwpad_plugin import qwpad_adapter

    class ExpiredClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_login_status(self):
            return {
                "errorCode": 0,
                "errorMsg": "",
                "userId": "1001",
                "userOnlineStatus": 2,
            }

        async def fetch_events(self, *args, **kwargs):
            raise QWPadAuthError("TOKEN 已失效")

        async def close(self):
            return None

    class Cursor:
        def __init__(self, directory, fingerprint=""):
            pass

        def load(self):
            return 1

        def save(self, event_id):
            pass

    monkeypatch.setattr(qwpad_adapter, "QWPadClient", ExpiredClient)
    monkeypatch.setattr(qwpad_adapter, "CursorStore", Cursor)
    adapter = QWPadAdapter(
        {"id": "qwpad-expired", "token": "wxa_expired"}, {}, asyncio.Queue()
    )

    async def run():
        with pytest.raises(QWPadAuthError, match="TOKEN 已失效"):
            await asyncio.wait_for(adapter.run(), timeout=2)

    asyncio.run(run())
    assert adapter.status == PlatformStatus.ERROR
    assert adapter._fatal_error is not None
    # TOKEN 占用必须释放，便于修复 TOKEN 后重新启用。
    from qwpad_plugin.qwpad_storage import token_fingerprint

    assert token_fingerprint(adapter.token) not in QWPadAdapter._ACTIVE_TOKENS
    # 实例保留到 terminate：否则 Web API 拿不到 last_error，排障时只看到“未找到实例”。
    assert adapter.instance_id in QWPadAdapter.instances
    assert adapter.get_stats()["last_error"] is not None

    asyncio.run(adapter.terminate())
    assert adapter.instance_id not in QWPadAdapter.instances


def test_send_streaming_aggregates_without_double_metric(monkeypatch):
    """流式输出只调用一次 send()，不能再走基类 send_streaming 重复上报 metric。"""
    adapter = _adapter()
    inbox = parse_event(_dm_item(), self_id="999", self_nick="小助理")
    event = adapter.create_event(adapter._build_message(inbox, "张三", []))
    sent = []

    async def fake_send(chain):
        sent.append(chain)

    async def base_streaming_should_not_run(*args, **kwargs):
        raise AssertionError("不应调用基类 send_streaming（会重复上报 metric）")

    monkeypatch.setattr(event, "send", fake_send)
    monkeypatch.setattr(
        AstrMessageEvent, "send_streaming", base_streaming_should_not_run
    )

    async def chunks():
        yield MessageChain([Plain("第一段")])
        yield MessageChain([Plain("第二段")])

    asyncio.run(event.send_streaming(chunks()))
    assert len(sent) == 1
    assert [c.text for c in sent[0].chain if isinstance(c, Plain)] == [
        "第一段",
        "第二段",
    ]


def test_record_path_resolves_tts_url(monkeypatch, tmp_path):
    """核心 TTS 的 Record(file=http...) 必须经 convert_to_file_path 落成本地文件。"""
    wav = tmp_path / "tts.wav"
    wav.write_bytes(b"RIFF")

    async def fake_convert(self):
        return str(wav)

    monkeypatch.setattr(Record, "convert_to_file_path", fake_convert)
    record = Record(file="http://127.0.0.1:6185/api/file/token")
    assert asyncio.run(_record_path(record)) == str(wav)


class FakeVoiceClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.voices = []
        self.uploads = []

    async def upload_file(self, path, file_type):
        self.uploads.append((path, file_type))
        return {"fileId": "voice-1"}

    async def send_voice(self, to_id, upload, voice_time, reply=None):
        self.voices.append((to_id, upload, voice_time, reply))
        return {"msgUniqueIdentifier": "voice-sent"}


def test_send_chain_sends_record_from_url(monkeypatch, tmp_path):
    """TTS 语音（URL）出站必须真的发成语音，而不是静默失败。"""
    from qwpad_plugin import qwpad_send

    wav = tmp_path / "tts.wav"
    wav.write_bytes(b"RIFF")

    async def fake_convert(self):
        return str(wav)

    async def fake_encode(path):
        assert path == str(wav)
        return b"\x02silk", 2

    monkeypatch.setattr(Record, "convert_to_file_path", fake_convert)
    monkeypatch.setattr(qwpad_send, "encode_voice_to_silk", fake_encode)

    client = FakeVoiceClient()
    stats = asyncio.run(send_chain(client, "1001", [Record(file="https://x/y.mp3")]))

    assert stats["success"] is True and stats["sent"] == 1
    assert client.voices and client.voices[0][2] == 2


def test_send_chain_keeps_at_only_message():
    client = FakeClient()
    stats = asyncio.run(send_chain(client, "room-1", [At(qq="123")]))
    assert stats["sent"] == 1
    assert {"subtype": 1, "text": "123"} in client.texts[0]["content"]


def test_send_chain_rolls_back_failed_count_after_dequote_retry():
    class ReplyFailClient(FakeClient):
        async def send_hyper_text(self, to_id, content, reply=None):
            if reply is not None:
                raise QWPadError("quote failed", retryable=False)
            return await super().send_hyper_text(to_id, content, reply=reply)

    client = ReplyFailClient()
    stats = asyncio.run(
        send_chain(
            client,
            "1001",
            [Plain("收到")],
            reply_anchor={"msgUniqueIdentifier": "orig-1"},
        )
    )
    assert stats["failed"] == 0
    assert stats["sent"] == 1
    assert stats["success"] is True


def test_cursor_store_invalidates_on_token_change(tmp_path):
    first = CursorStore(tmp_path, "fp-a")
    first.save(42)
    assert CursorStore(tmp_path, "fp-a").load() == 42
    # 换了 TOKEN：旧游标属于另一个账号的事件空间，必须重置。
    assert CursorStore(tmp_path, "fp-b").load() == 0


def test_poll_loop_skips_poison_event(monkeypatch):
    adapter = _adapter()
    adapter._cursor_store = None

    class PageClient:
        def __init__(self):
            self.calls = 0

        async def fetch_events(self, cursor, **kwargs):
            self.calls += 1
            if self.calls <= 3:
                return SimpleNamespace(items=[{"id": 5}], next_id=5)
            adapter._running = False
            return SimpleNamespace(items=[], next_id=5)

    async def always_fail(item):
        raise RuntimeError("bad message")

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(adapter, "_handle_event_item", always_fail)
    monkeypatch.setattr(qwpad_adapter.asyncio, "sleep", no_sleep)
    adapter._client = PageClient()
    adapter._running = True

    asyncio.run(adapter._poll_loop())
    assert adapter._cursor == 5
    assert adapter._event_failures == {}


def test_poll_loop_stops_on_non_retryable_error():
    adapter = _adapter()

    class BadRequestClient:
        async def fetch_events(self, cursor, **kwargs):
            raise QWPadApiError("bad cursor", retryable=False)

    adapter._client = BadRequestClient()
    adapter._running = True
    asyncio.run(adapter._poll_loop())
    assert adapter._running is False
    assert adapter._fatal_error is not None
    assert adapter._stop_event.is_set()


def test_reply_component_uses_quoted_message_attribution():
    adapter = _adapter()
    item = _group_item()
    item["message"]["structured"]["msgData"]["reply"] = {
        "msgUniqueIdentifier": "orig-9",
        "userId": "1002",
        "showName": "李四",
        "timeStamp": 1792627000,
        "msgData": {"content": "<b>上一条</b>"},
    }
    inbox = parse_event(item, self_id="999", self_nick="小助理")
    assert inbox is not None
    msg = adapter._build_message(inbox, "测试群", [])
    reply = next(c for c in msg.message if isinstance(c, Reply))
    assert reply.id == "orig-9"
    # pydantic 会把纯数字字符串转成 int；语义上仍是被引用消息的发送者。
    assert str(reply.sender_id) == "1002"
    assert reply.sender_nickname == "李四"
    assert reply.message_str == "上一条"


def test_nickname_mention_requires_boundary():
    item = _dm_item()
    item["message"]["structured"]["msgData"] = {"content": "@小助理2 你好"}
    assert parse_event(item, self_id="999", self_nick="小助理").mentions_me is False

    item["message"]["structured"]["msgData"] = {"content": "@小助理 你好"}
    assert parse_event(item, self_id="999", self_nick="小助理").mentions_me is True


def test_location_fallback_uses_core_fields():
    client = FakeClient()
    asyncio.run(
        send_chain(
            client,
            "1001",
            [Location(lat=31.2, lon=121.5, title="门店", content="南京路")],
        )
    )
    assert "31.2,121.5" in client.texts[0]["content"]
    assert "南京路" in client.texts[0]["content"]
