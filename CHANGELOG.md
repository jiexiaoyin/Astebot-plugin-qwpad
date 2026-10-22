# Changelog

## 0.2.1 — 2026-10-22

对照 QWPad OpenAPI（`https://api.qwpad.com/openapi.json`）逐项校正。

### 修复

- **在线判定**：`errorCode=0` 只表示状态查询成功，真正在线必须
  `userOnlineStatus=2`。启动时账号不在线会明确报错（含具体状态值），运行时
  掉线告警也只按该字段判断；状态接口新增 `account.online`。
- **下载鉴权隔离**：`cloudUrl` 属于对象存储，改用独立的无鉴权 HTTP 客户端下载，
  不再把业务 TOKEN 发给第三方 CDN；`fs://` 对象引用会给出明确错误（存储未开启
  HTTP 出口），不再拼 TOKEN/AES/fileId。
- **大文件出站**：普通 CDN 上传要求文件非空且小于 20MiB。视频/文件达到 20MiB
  自动改走 `cdnBigFileUpload`（`fileType` 22/35）→ `sendBigFile`/`sendBigVideo`；
  图片没有大文件接口，超限直接报错并提示压缩。
- **上传内存占用**：普通上传改为文件句柄流式提交，不再整个读入内存；上传结果
  缺失 `fileSize`/`fileMd5` 时本地补齐，避免发出空必填字段。
- **视频元数据**：`sendVideo` 的 `duration`/`coverImageSize` 优先复制同次上传的
  `durationTime`/`fileThumbSize`，ffprobe 仅作兜底。
- **引用窗口**：`QUOTE_WINDOW_S` 真正生效，过期锚点不再被引用。
- **分发重试**：消息改为分发成功后才记入去重；分发抛错时游标不前进，下一轮
  可正常重试，不再因提前去重而丢消息。

### 新增

- **入站语音转文字**：调用 QWPad `/msg/voiceToTextApply` + `/msg/voiceToTextQuery`，
  用消息的 `msgServerId`/`seq` 提交任务并轮询结果；识别文本作为正文交给模型，
  转写成功时不再附带 WAV（避免模型尝试读取二进制/执行 shell 解析音频），失败时
  保留语音附件。转写失败/超时（20 秒）只记 WARN，不阻塞消息；可用
  `voice_to_text=false` 关闭。此前 AstrBot 未配置 STT 时，模型只能拿到
  `Record` 组件，无法理解语音内容。

### 测试

- 新增客户端契约测试与适配器/发送回归测试，覆盖下载不带 TOKEN、`fs://`、
  BigCDN 上传与发送、视频元数据、20MiB 分流、语音转文字、在线状态判定、
  引用过期、分发失败重试。

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
- 完善项目文档：README 增加配置参考、准入规则、使用说明、状态接口、排障与 FAQ；
  DEPLOY 增加安装、依赖检查、验证、0.1→0.2 迁移与回滚步骤。

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
