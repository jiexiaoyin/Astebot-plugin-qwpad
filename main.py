"""astrbot_plugin_qwpad 插件入口。

官方平台适配器插件范式（docs/zh/dev/plugin-platform-adapter.md）：
  - main.py 必须有 Star 类；
  - 平台适配器放独立文件，用 @register_platform_adapter 装饰；
  - 必须在 main 模块顶层 import 适配器模块，装饰器才会执行注册。

消息流：企业微信 → QWPad /events → 本插件 → AstrBot → QWPad /msg/* → 企业微信
"""

from astrbot import logger
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

# 触发平台适配器注册（AstrBot 以 namespace package 加载插件目录，
# 不会自动执行包内其他模块的 import）。
from . import qwpad_adapter  # noqa: F401
from .qwpad_adapter import QWPadAdapter

PLUGIN_NAME = "astrbot_plugin_qwpad"


class QwpadPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self.web_status,
            ["POST"],
            "查询 QWPad 平台实例连接状态（不含 TOKEN）",
        )

    async def web_status(self):
        """返回所有 QWPad 实例的在线状态、游标、策略与计数。

        可选 body: ``{"platform_id": "xxx"}``。
        """
        payload = await request.json(default={})
        payload = payload if isinstance(payload, dict) else {}
        platform_id = str(payload.get("platform_id") or "").strip()

        instances = []
        for instance_id, adapter in list(QWPadAdapter.instances.items()):
            if platform_id and instance_id != platform_id:
                continue
            stats = adapter.get_stats()
            instances.append(
                {
                    "platform_id": instance_id,
                    "connected": stats.get("connected", False),
                    "status": stats.get("status"),
                    "account": stats.get("account"),
                    "cursor": stats.get("cursor"),
                    "dedupe_size": stats.get("dedupe_size"),
                    "messages_in": stats.get("messages_in"),
                    "messages_out": stats.get("messages_out"),
                    "last_event_at": stats.get("last_event_at"),
                    "policy": stats.get("policy"),
                    "last_error": stats.get("last_error"),
                }
            )
        if platform_id and not instances:
            return error_response("未找到该 QWPad 平台实例（可能未启用或未连接）")
        return json_response(
            {
                "instances": instances,
                "count": len(instances),
                "hint": "connected=false 时查看 last_error；TOKEN 不会通过接口返回。",
            }
        )

    async def terminate(self) -> None:
        logger.info("[QWPad] 插件卸载，平台适配器生命周期由 AstrBot 管理")
