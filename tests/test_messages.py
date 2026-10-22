from qwpad_plugin.qwpad_messages import (
    parse_event,
    reply_context,
    split_flattened_quote,
)


def _event(
    *,
    msg_type: int = 0,
    msg_data: dict | None = None,
    sender: str = "1001",
    room: str = "0",
    sender_name: str = "张三",
    category: str = "chat",
    outer_type: int = 15000,
    event_id: int = 7,
):
    return {
        "id": event_id,
        "type": outer_type,
        "display": {
            "category": category,
            "senderId": sender,
            "roomId": room,
            "senderName": sender_name,
            "messageId": "disp-1",
            "parseStatus": "parsed",
            "messageType": {"nativeName": "kText"},
        },
        "message": {
            "msgType": msg_type,
            "structured": {
                "senderId": sender,
                "fromRoomId": room,
                "senderName": sender_name,
                "msgType": msg_type,
                "msgUniqueIdentifier": "uniq-1",
                "timestamp": 1792627702,
                "msgData": msg_data or {"content": "你好"},
            },
        },
    }


def test_parse_dm_text():
    inbox = parse_event(_event(), self_id="999", self_nick="小助理")
    assert inbox is not None
    assert inbox.chat_type == "dm"
    assert inbox.chat_id == "1001"
    assert inbox.text == "你好"
    assert inbox.message_id == "uniq-1"
    assert inbox.timestamp == 1792627702


def test_parse_group_mention():
    item = _event(
        room="room-1",
        msg_data={"content": "@小助理 看下库存", "atList": [{"userId": "999"}]},
    )
    inbox = parse_event(item, self_id="999", self_nick="小助理")
    assert inbox is not None
    assert inbox.chat_type == "group"
    assert inbox.chat_id == "room-1"
    assert inbox.mentions_me is True
    assert inbox.text == "看下库存"


def test_non_chat_category_dropped():
    assert parse_event(_event(category="system"), self_id="999") is None


def test_non_chat_outer_type_dropped():
    assert parse_event(_event(outer_type=15500), self_id="999") is None


def test_flattened_quote():
    quote, body = split_flattened_quote('"昨天的报表"\n------\n今天再发一份')
    assert quote == "昨天的报表"
    assert body == "今天再发一份"
    inbox = parse_event(
        _event(msg_data={"content": '"昨天的报表"\n------\n今天再发一份'}),
        self_id="999",
    )
    assert inbox is not None
    assert inbox.text == "今天再发一份"
    assert reply_context(inbox) == "昨天的报表"


def test_image_media_ref():
    item = _event(
        msg_type=1,
        msg_data={
            "fileId": "fid-1",
            "fileAeskey": "aes-1",
            "fileSize": 1234,
            "fileMd5": "md5-1",
            "filename": "a.jpg",
            "imageHasHd": 1,
        },
    )
    item["display"]["messageType"]["nativeName"] = "kImage"
    item["display"]["parseStatus"] = "parsed_media"
    inbox = parse_event(item, self_id="999")
    assert inbox is not None
    assert inbox.media is not None
    assert inbox.media.kind == "image"
    assert inbox.media.file_id == "fid-1"
    assert inbox.media.aes_key == "aes-1"
    assert inbox.media.file_type == 1
    assert inbox.text == "[图片]"


def test_self_message_flagged():
    inbox = parse_event(_event(sender="999"), self_id="999")
    assert inbox is not None
    assert inbox.is_self is True
