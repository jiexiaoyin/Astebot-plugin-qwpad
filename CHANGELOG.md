# Changelog

## 0.1.0 — 2026-10-22

首个可用版本。

### 新增

- QWPad 平台适配器（`@register_platform_adapter("qwpad")`），单 TOKEN 单实例
- 长轮询事件接收：游标持久化、跨重启去重、首次启动回看窗口
- 入站解析：文本/富文本、卡片、链接、位置、合并转发、引用、图片/语音/视频/文件
- 出站：文本分片、@ 成员 / @所有人、引用回复、图片、语音（SILK）、视频、文件
- 语音：入站 SILK→WAV；出站 ffmpeg→PCM→SILK，超限自动降码率
- 策略：用户/群白名单、黑名单、`group_reply` 三种群模式、默认拒绝
- 状态 Web API：`POST /astrbot-plugin-qwpad/status`（不返回 TOKEN）
- 流式输出聚合为单条发送（企业微信无消息编辑能力）

### 修复

- 不再覆盖父类 `Platform._started_at`（其类型为 `datetime | None`），
  修复状态接口因 `float.isoformat()` 报 500 的问题；补充回归测试。
- `event.send` 直发路径未计入 `messages_out`；现在按实际发送条数上报并记录发送日志。
- 修复 `event.send` 异常分支里的局部 `from astrbot import logger` 造成的函数级变量遮蔽
  （每次发送都报 `UnboundLocalError`，虽不影响送达但污染日志）。
