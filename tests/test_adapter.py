import asyncio

from astrbot.api.message_components import At, Reply

from qwpad_plugin.qwpad_adapter import QWPadAdapter
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
    async def get_room_detail(self, room_id):
        return {"roomName": "测试群"}

    async def batch_get_userinfo(self, user_ids):
        return {"contactList": []}


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


def test_get_stats_follows_base_contract():
    adapter = _adapter()
    stats = adapter.get_stats()
    assert stats["id"] == "qwpad-test"
    # 父类 _started_at 是 datetime | None，聚合统计不能把它当成 float 覆盖。
    assert stats["started_at"] is None
    assert stats["connected"] is False
    assert stats["policy"]["allow_all_users"] is True


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
