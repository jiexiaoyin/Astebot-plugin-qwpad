# Changelog

## 0.2.0 — 2026-10-22

### 变更

- 适配器显示名和平台类型统一为 **wecom(Pad)**（原 `qwpad`）。AstrBot 4.29 的机器人
  列表/新增下拉直接显示适配器注册名和配置 `id`，因此 `type`、`id` 一并改名；
  升级时需把已有配置的 `type`/`id` 从 `qwpad` 改为 `wecom(Pad)`。
- 明确账号模型：**一个平台实例 = 一个 TOKEN = 一个企业微信账号**。多账号通过
  添加多个平台实例实现（`id` 唯一、各自 TOKEN）；插件不提供「一个 TOKEN 下挂
  多个账号」的配置。
- 同一 TOKEN 被重复实例使用会在启动时拒绝，避免互抢事件与游标。
- 新增企业微信 logo（`logo.png`），机器人列表和新增下拉显示企业微信图标。

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
