import asyncio

from astrbot.api.message_components import At, AtAll, Plain

from qwpad_plugin.qwpad_send import send_chain, split_message, strip_markdown


class FakeClient:
    def __init__(self):
        self.texts = []
        self.hyper = []

    async def send_text(self, to_id, content, reply=None):
        self.texts.append({"to": to_id, "content": content, "reply": reply})
        return {"msgServerId": "1", "msgUniqueIdentifier": "m1"}

    async def send_hyper_text(self, to_id, content, reply=None):
        self.hyper.append({"to": to_id, "content": content, "reply": reply})
        return {"msgServerId": "2", "msgUniqueIdentifier": "m2"}


def test_split_message_hard_limit():
    pieces = split_message("a" * 4500, 2000)
    assert [len(p) for p in pieces] == [2000, 2000, 500]


def test_split_message_paragraphs():
    text = "\n".join(["x" * 900, "y" * 900, "z" * 900])
    pieces = split_message(text, 2000)
    assert len(pieces) == 2
    assert all(len(p) <= 2000 for p in pieces)


def test_strip_markdown():
    assert strip_markdown("**粗体** 和 `代码`\n# 标题\n[链接](https://a.b)") == (
        "粗体 和 代码\n标题\n链接 (https://a.b)"
    )


def test_send_chain_plain():
    client = FakeClient()
    stats = asyncio.run(
        send_chain(client, "chat-1", [Plain("你好")], limit=2000)
    )
    assert stats["success"] is True
    assert client.texts[0]["content"] == "你好"
    assert client.texts[0]["reply"] is None


def test_send_chain_at_uses_hypertext():
    client = FakeClient()
    asyncio.run(
        send_chain(
            client,
            "room-1",
            [At(qq="1001", name="张三"), Plain(" 请看")],
            limit=2000,
        )
    )
    assert not client.texts
    segments = client.hyper[0]["content"]
    assert {"subtype": 1, "text": "1001"} in segments
    assert segments[-1] == {"subtype": 0, "text": "请看"}


def test_send_chain_at_all():
    client = FakeClient()
    asyncio.run(
        send_chain(client, "room-1", [AtAll(), Plain("通知")], limit=2000)
    )
    assert {"subtype": 1, "text": "0"} in client.hyper[0]["content"]


def test_send_chain_quote_first_piece():
    client = FakeClient()
    anchor = {
        "msgUniqueIdentifier": "orig-1",
        "showName": "张三",
        "timeStamp": 1,
        "type": 0,
        "userId": "1001",
        "msgData": {"content": "原文"},
    }
    long_text = "a" * 2500
    asyncio.run(
        send_chain(client, "chat-1", [Plain(long_text)], reply_anchor=anchor, limit=2000)
    )
    # 首片走 hypertext（空串 + 正文 + 引用），第二片普通文本且不引用。
    assert client.hyper[0]["reply"]["msgUniqueIdentifier"] == "orig-1"
    assert client.hyper[0]["content"][0] == {"subtype": 0, "text": ""}
    assert len(client.hyper[0]["content"][1]["text"]) == 2000
    assert client.texts[0]["content"] == long_text[2000:]
    assert client.texts[0]["reply"] is None
