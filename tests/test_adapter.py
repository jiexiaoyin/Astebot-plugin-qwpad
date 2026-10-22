import asyncio

import pytest
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain, Reply

from qwpad_plugin.qwpad_adapter import (
    QUOTE_WINDOW_S,
    QWPadAdapter,
    account_online_state,
)
from qwpad_plugin.qwpad_messages import parse_event
from qwpad_plugin.qwpad_policy import PolicySnapshot


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
