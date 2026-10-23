"""QWPad 平台消息事件类。

AstrBot 约定：
  - 基类 ``AstrMessageEvent.send()`` 只上报 metric，平台必须重写才能真正发送；
  - 发送逻辑走 ``qwpad_send.send_chain()``（``event.send`` / ``send_by_session`` 共用）；
  - 最后调用 ``super().send(message)`` 完成基类 metric 上报。

QWPad 没有消息编辑能力，因此 ``send_streaming`` 把整个流聚合后只发一次。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .qwpad_send import send_chain

if TYPE_CHECKING:
    from .qwpad_adapter import QWPadAdapter
    from .qwpad_client import QWPadClient


class QWPadMessageEvent(AstrMessageEvent):
    def __init__(
        self,
        *args: Any,
        client: QWPadClient | None = None,
        adapter: QWPadAdapter | None = None,
        chat_id: str = "",
        reply_anchor: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._client = client
        self._adapter = adapter
        self.chat_id = chat_id or self.session.session_id
        self._reply_anchor = reply_anchor

    async def send(self, message: MessageChain) -> None:
        stats: dict[str, Any] | None = None
        try:
            if self._client is not None:
                adapter = self._adapter
                stats = await send_chain(
                    self._client,
                    self.chat_id,
                    list(message.chain),
                    reply_anchor=self._reply_anchor,
                    limit=adapter.max_message_length if adapter else 2000,
                    strip_md=adapter.strip_markdown if adapter else True,
                )
                # 引用只用于本次回复的第一条出站消息。
                self._reply_anchor = None
                if adapter is not None and stats.get("sent"):
                    adapter.record_outbound(stats["sent"])
                    logger.info(
                        "[QWPad:%s] 已发送 %s 条 -> %s",
                        adapter.instance_id,
                        stats["sent"],
                        self.chat_id,
                    )
        finally:
            await super().send(message)
        if not stats:
            return
        if stats.get("failed") and not stats.get("sent"):
            self.set_extra("_qwpad_last_send_errors", stats.get("errors"))
            detail = "; ".join(str(e) for e in stats.get("errors", [])[:3])
            # 全部失败必须让核心感知，否则会被当成“已回复”
            raise RuntimeError(f"QWPad 发送全部失败: {detail or 'unknown error'}")
        if stats.get("failed"):
            self.set_extra("_qwpad_last_send_errors", stats.get("errors"))
            logger.warning(
                "[QWPad:%s] 部分消息发送失败: %s",
                getattr(self._adapter, "instance_id", "-"),
                stats.get("errors"),
            )

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        """聚合流式输出后一次性发送（QWPad 无法编辑已发出的消息）。"""
        combined: MessageChain | None = None
        async for chain in generator:
            if combined is None:
                combined = chain
            else:
                combined.chain.extend(chain.chain)
        if combined is not None:
            # self.send() 内部已经调用 super().send() 完成 metric 上报；
            # 这里不能再调用 super().send_streaming()，否则同一条回复会上报两次
            # msg_event_tick（且生成器已被本方法消费完）。
            await self.send(combined)


__all__ = ["QWPadMessageEvent"]
