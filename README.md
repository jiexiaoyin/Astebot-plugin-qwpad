# astrbot_plugin_qwpad

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 的企业微信 **pad 协议**平台适配器。
它通过 [QWPad](https://api.qwpad.com) 的 OpenAPI 长轮询收发消息，让 AstrBot 直接以
企业微信账号身份与同事、客户对话，**不需要公网 IP、不需要配置回调地址**。

当前版本：`0.3.0` · 适用 AstrBot `>= 4.16.0, < 5`（已在 4.29.1 验证）

- 仓库：<https://github.com/jiexiaoyin/Astebot-plugin-qwpad>
- 许可证：MIT

> ⚠️ **一个 QWPad TOKEN 对应一个企业微信账号，同一 TOKEN 同时只能有一个轮询者。**
> 启用本插件前，请先停用 Hermes 或其他程序里消费同一 TOKEN 的 qwpad 插件，
> 否则两边会抢事件、互相覆盖游标，表现为消息丢失或重复回复。

---

## 目录

1. [项目说明](#1-项目说明)
2. [核心概念](#2-核心概念)
3. [功能清单](#3-功能清单)
4. [环境要求](#4-环境要求)
5. [安装](#5-安装)
6. [快速开始](#6-快速开始)
7. [配置参考](#7-配置参考)
8. [准入规则详解](#8-准入规则详解)
9. [使用说明](#9-使用说明)
10. [多账号](#10-多账号)
11. [状态接口](#11-状态接口)
12. [数据与状态文件](#12-数据与状态文件)
13. [日志与排障](#13-日志与排障)
14. [架构](#14-架构)
15. [升级、迁移与回滚](#15-升级迁移与回滚)
16. [安全建议](#16-安全建议)
17. [已知限制](#17-已知限制)
18. [开发与测试](#18-开发与测试)
19. [FAQ](#19-faq)

---

## 1. 项目说明

QWPad 把企业微信的 pad 协议封装成 HTTP API。本插件是 AstrBot 的 `Platform`
适配器，负责：

- 用 `Authorization: Bearer <TOKEN>` 调用 QWPad；
- 长轮询 `GET /events` 拉取私聊/群聊事件，解析成 AstrBot 消息；
- 把 AstrBot 生成的 `MessageChain` 转成企业微信的文本、@、引用、图片、语音、
  视频、文件并发送；
- 维护每实例的游标、去重、白名单、收发统计与在线状态。

消息链路：

```text
企业微信 ⇄ QWPad ⇄ 本插件 ⇄ AstrBot（事件总线 / LLM / 插件）⇄ 本插件 ⇄ QWPad ⇄ 企业微信
```

适合的场景：

- 把企业微信客服/内部助手接入 AstrBot 的 LLM、知识库、工具插件；
- 多个企业微信账号分别接入不同的 AstrBot 平台实例；
- 在不开放公网端口的前提下收消息（长轮询出站连接）。

## 2. 核心概念

### 2.1 一个 TOKEN = 一个账号 = 一个平台实例

QWPad 的 `wxa_` TOKEN 与企业微信账号一一对应。插件按平台实例运行，每个实例：

- 一个 `token`；
- 一套游标、去重、白名单/群策略；
- 一份独立的收发统计与状态。

多账号 = 在 AstrBot 里添加多个 `wecom(Pad)` 平台实例，各自填不同 TOKEN。
**不存在「一个 TOKEN 下挂多个账号」的配置。**

同一进程内，插件用 TOKEN 的 SHA-256 指纹做占用锁：后启动的重复实例会直接失败，
日志提示 `同一 TOKEN 已被平台实例 ... 使用`。

### 2.2 准入 ≠ 唤醒

插件只决定「这条消息要不要进入 AstrBot 事件流」，不决定「AI 要不要回复」。

- 私聊：通过白名单后进入事件流，按 AstrBot 正常规则唤醒 LLM。
- 群聊 `atbot`：只有真正 @ 了机器人账号的消息才进入；插件会在消息链里补一个
  `At(self_id)`，让 AstrBot 核心把消息判给机器人。
- 群聊 `all`：所有群消息都进入事件流，但未 @ 的消息**不会伪造 At**，是否回复由
  AstrBot 的唤醒规则或主动回复插件决定。
- 群聊 `none`：所有群消息直接丢弃。

### 2.3 默认拒绝

`allow_all_users=false` 且 `allow_users`、`allow_groups` 都为空时，
**所有消息都会被拒绝**，插件只保持连接。这是安全的初始状态，启动时会打印 WARN。
配置好白名单后再放开流量。

### 2.4 长轮询与游标

插件调用 `GET /events?afterId=<cursor>&limit=<n>&waitSeconds=<s>&format=unified`：

- 服务端没有新事件时挂起最多 `poll_wait_seconds` 秒，然后返回空页；
- 每处理完一条事件，游标前进并原子写入 `cursor.json`；
- 进程正常重启后从游标继续，不会重放已处理消息；
- 首次启动（没有游标）时，`startup_backlog_minutes` 决定只处理最近多少分钟的消息；
- 网络错误按 1s → 2s → … → 60s 指数退避重试；TOKEN 失效则停止轮询并记录错误。

## 3. 功能清单

### 3.1 收消息

- 私聊、群聊长轮询；自动丢弃系统通知、账号/设备状态、已读回执、撤回提示等非聊天事件；
- 文本、富文本、Markdown、链接、位置、名片/卡片、合并转发、引用消息；
- 图片、表情、语音、视频、文件；合并转发中的媒体也会尝试下载；
- 入站语音 SILK → WAV（需要 `pilk`），并调用 QWPad
  `/msg/voiceToTextApply|Query` 自动转文字；转写成功时只把文本交给模型（不再
  附带 WAV，避免模型尝试读取二进制），失败时保留语音附件，不影响消息处理
  （可用 `voice_to_text=false` 关闭）；
- 群聊 @ 识别、发送者昵称/群名查询（带缓存，查询失败不影响消息）。

### 3.2 发消息

- 文本分片（默认 2000 字符，按换行优先切分）；
- 发送前清洗 Markdown（企业微信不渲染 Markdown）；
- @ 成员 / @所有人、引用回复；
- 图片、语音（任意音频 → SILK）、视频、文件上传后发送；
- 视频/文件 ≥20MiB 自动改走 BigCDN（`cdnBigFileUpload` → `sendBigFile/sendBigVideo`）；
- 同一会话的发送串行化，避免多条回复的分片互相穿插；
- 流式输出自动聚合为一条消息（企业微信不支持消息编辑）。

### 3.3 策略与多实例

- 用户/群白名单、用户/群黑名单；
- `group_reply` 三种群模式：`atbot` / `all` / `none`；
- `allow_all_users` 调试开关；
- 每实例独立游标、去重、统计；
- 同一 TOKEN 重复实例启动即拒绝。

### 3.4 可观测性

- 结构化启动/收发/错误日志（不含 TOKEN 明文）；
- 插件 Web API 查询在线状态、账号、游标、策略、计数、最近错误；
- AstrBot 平台状态页可看到 `wecom(Pad)` 实例。

## 4. 环境要求

| 组件 | 要求 |
| --- | --- |
| AstrBot | `>= 4.16.0, < 5`，已在 `4.29.1` 验证 |
| Python | 3.10+（跟随 AstrBot 运行环境） |
| Python 包 | `httpx>=0.28,<1`、`pilk>=0.2.4,<1`（见 `requirements.txt`；AstrBot 镜像已自带） |
| 系统命令 | `ffmpeg` / `ffprobe`（出站语音、视频时长；缺失时语音降级为文件） |
| QWPad | 已开通业务 TOKEN（`wxa_` 前缀），对应企业微信账号已完成扫码登录并在线 |
| 网络 | AstrBot 能访问 `api_base`（默认 `https://api.qwpad.com`）；无需公网入站 |

检查依赖（也可 `pip install -r requirements.txt`）：

```bash
# 容器名按实际替换
docker exec astrbot sh -c 'command -v ffmpeg; command -v ffprobe; \
  python -c "import httpx, pilk; print(\"python deps ok\")"'
```

如果缺少 `ffmpeg`，可在镜像/容器内安装；不安装也能运行，只是出站语音会退化为
普通文件，视频时长可能取不到。

## 5. 安装

### 5.1 从仓库安装（推荐）

AstrBot Dashboard → 插件管理 → 安装插件 → 填入仓库地址：

```text
https://github.com/jiexiaoyin/Astebot-plugin-qwpad
```

安装后在插件列表确认版本为 `0.3.0`，并重启 AstrBot 或重载插件。

### 5.2 手动部署

```bash
# ASTRBOT_DATA 例如 /opt/astrbot/data 或 Docker 挂载出来的 data 目录
rsync -a --delete \
  --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  ./ "$ASTRBOT_DATA/plugins/astrbot_plugin_qwpad/"

docker restart astrbot   # 或重启你的 AstrBot 进程
```

插件目录名保持 `astrbot_plugin_qwpad`。升级时不要删除
`$ASTRBOT_DATA/plugin_data/astrbot_plugin_qwpad/`，否则游标和运行状态会丢失。

### 5.3 确认注册成功

启动日志应出现：

```text
Loading plugin astrbot_plugin_qwpad ...
Plugin astrbot_plugin_qwpad (0.3.0) ...
Loading IM platform adapter wecom(Pad)(wecom(Pad)) ...
```

Dashboard「平台配置 → 新增」中应能看到 **wecom(Pad)**。

## 6. 快速开始

### 步骤 1：准备 TOKEN 与在线账号

1. 在 QWPad 控制台确认目标企业微信账号已登录且在线；
2. 复制该账号对应的业务 TOKEN（`wxa_` 开头）；
3. **停用其他消费同一 TOKEN 的程序**（例如 Hermes 的 qwpad 插件）。

### 步骤 2：安装并重启

按[第 5 节](#5-安装)安装插件，重启 AstrBot，确认 `wecom(Pad)` 出现在平台列表。

### 步骤 3：添加平台实例

Dashboard → 平台配置 → 新增 → **wecom(Pad)**：

| 字段 | 示例 | 说明 |
| --- | --- | --- |
| `id` | `wecom(Pad)` | 平台实例唯一标识；多账号时必须不同 |
| `token` | `wxa_xxx` | 该账号的 QWPad TOKEN |
| `api_base` | `https://api.qwpad.com` | 私有化部署才需要改 |
| `allow_users` | `zhangsan,lisi` | 允许私聊的成员 userId |
| `allow_groups` | `roomid1,roomid2` | 允许进入的群 roomId |
| `group_reply` | `atbot` | 群里只处理 @ 机器人的消息 |

保存并启用。平台适配器会自动启动，不需要额外命令。

### 步骤 4：看日志确认连接

```bash
docker logs --since 3m astrbot 2>&1 | grep -iE 'qwpad|wecom\(Pad\)'
```

期望：

```text
[QWPad:wecom(Pad)] 已连接 <昵称>（<企业> / <userId>），游标 <n>，策略 ...
```

如果看到「白名单为空且 allow_all_users=false」，说明还在默认拒绝状态，继续下一步。

### 步骤 5：发消息验证

1. 用白名单内的成员私聊机器人，应收到回复；
2. 在 `allow_groups` 内的群里 @ 机器人，应收到回复；
3. 调用[状态接口](#11-状态接口)确认 `connected=true`、`messages_in/messages_out` 增长。

## 7. 配置参考

AstrBot Dashboard → 平台配置 → `wecom(Pad)` 实例。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `token` | string | — | QWPad 业务 TOKEN（`wxa_` 前缀）。**必填**，一个 TOKEN 一个账号 |
| `api_base` | string | `https://api.qwpad.com` | QWPad API 地址；私有化部署时修改 |
| `allow_users` | string | 空 | 用户白名单，userId 逗号/空格/换行分隔。私聊放行；群聊中该成员的消息也可放行 |
| `allow_groups` | string | 空 | 群白名单，roomId 逗号分隔。名单内群的消息可进入 |
| `block_users` | string | 空 | 用户黑名单，优先级最高，私聊/群聊都生效 |
| `block_groups` | string | 空 | 群黑名单，优先级高于群白名单 |
| `allow_all_users` | bool | `false` | 放开所有聊天（仍受黑名单约束，非聊天事件仍丢弃）；仅调试/内部账号 |
| `group_reply` | string | `atbot` | `atbot`=只放行 @ 我的群消息；`all`=放行全部群消息；`none`=忽略所有群消息 |
| `quote_replies` | bool | `true` | 回复时引用触发消息；仅文本/富文本原消息可引用 |
| `voice_to_text` | bool | `true` | 入站语音调用 QWPad 转文字；失败时保留语音附件，不影响消息。若已启用 AstrBot 全局「语音识别」STT，建议设为 `false`，由 AstrBot 统一转写 |
| `strip_markdown` | bool | `true` | 发送前清洗 Markdown（粗体、标题、链接、代码等转纯文本） |
| `poll_wait_seconds` | int | `25` | long-poll 等待秒数，范围 1–30；越小越实时，请求越多 |
| `poll_limit` | int | `50` | 单次拉取事件上限，范围 1–500 |
| `max_message_length` | int | `2000` | 单条文本字符上限（最小 200），超长自动分片 |
| `startup_backlog_minutes` | int | `5` | 首次启动无游标时只处理最近 N 分钟；`0`=处理服务端全部积压 |

配置保存后，AstrBot 需要重载/重启该平台实例才会生效。修改 `token` 前先停用旧实例，
避免与新实例抢同一个 TOKEN。

### 7.1 最小配置

只允许成员 `zhangsan` 私聊、群 `roomid1` 内 @ 机器人：

```json
{
  "type": "wecom(Pad)",
  "id": "wecom(Pad)",
  "enable": true,
  "token": "wxa_xxx",
  "api_base": "https://api.qwpad.com",
  "allow_users": "zhangsan",
  "allow_groups": "roomid1",
  "group_reply": "atbot",
  "quote_replies": true,
  "voice_to_text": true,
  "strip_markdown": true,
  "poll_wait_seconds": 25,
  "poll_limit": 50,
  "max_message_length": 2000,
  "startup_backlog_minutes": 5
}
```

### 7.2 只允许群、不允许私聊

`allow_users` 留空，只填 `allow_groups`。此时私聊全部拒绝，群消息按 `group_reply` 处理。

### 7.3 群消息全量观察

```json
{
  "allow_groups": "roomid1",
  "group_reply": "all"
}
```

所有群消息进入事件流，但未 @ 的消息不会强制唤醒 LLM；可配合 AstrBot 的主动回复/
心流类插件使用。

### 7.4 仅内部调试

```json
{
  "allow_all_users": true
}
```

仅建议短期使用；黑名单仍然生效。

## 8. 准入规则详解

对每条已解析的聊天消息，按以下顺序判断：

1. 发送者在 `block_users` → **拒绝**；
2. 群消息且群在 `block_groups` → **拒绝**；
3. **群消息**：
   1. `group_reply=none` → **拒绝**；
   2. 未开启 `allow_all_users`，且群不在 `allow_groups`、发送者也不在 `allow_users`
      → **拒绝**；
   3. `group_reply=atbot` 且没有真正 @ 机器人 → **拒绝**；
   4. 否则**放行**；
4. **私聊**：`allow_all_users=true` 或发送者在 `allow_users` → **放行**，否则拒绝。

几个容易踩的点：

- **群白名单和成员白名单是「或」的关系**：只要发送者在 `allow_users`，他在任何群
  的消息都能通过第 3.2 步（`atbot` 模式仍需 @）。如果你只想开放某个群，请把
  `allow_users` 留空，只用 `allow_groups`。
- **黑名单永远优先**，且 `allow_all_users` 不能绕过黑名单。
- **只有真正的 @ 才算**：消息文本里手写「@机器人」但没有企业微信 mention 结构，
  不会被 `atbot` 放行。
- **准入通过不代表 AI 回复**：群 `all` 模式下未 @ 消息只是进入事件流，是否唤醒由
  AstrBot 的唤醒规则决定。

## 9. 使用说明

### 9.1 私聊

白名单内成员直接私聊机器人即可。插件会带上发送者昵称、消息类型和媒体；LLM 的回复
按 `max_message_length` 分片发送。

### 9.2 群聊 @

`group_reply=atbot`（默认）时，在群里 @ 机器人并输入内容。插件会在消息链中补
`At(self_id)`，AstrBot 才会把这条群消息判给机器人。

### 9.3 群聊观察模式

`group_reply=all` 时，群内所有消息都会进入事件流。未 @ 的消息不会自动唤醒 LLM，
但可以被需要上下文的插件使用。若希望未 @ 也主动回复，请配合 AstrBot 的主动回复
插件，并自行控制频率与白名单。

### 9.4 媒体消息

| 类型 | 入站 | 出站 |
| --- | --- | --- |
| 图片 | 下载后以 `Image` 交给模型 | 上传 → `sendImage` |
| 语音 | SILK → WAV（`pilk`）+ QWPad 语音转文字；成功只发文本，失败保留音频/文件 | `ffmpeg` → 24kHz PCM → SILK，≤60KB；超限自动降码率 |
| 视频 | 下载后以 `Video` 交给模型 | `<20MiB`：上传 → `sendVideo`；`≥20MiB`：BigCDN → `sendBigVideo` |
| 文件 | 下载后以 `File` 交给模型 | `<20MiB`：上传 → `sendFile`；`≥20MiB`：BigCDN → `sendBigFile` |
| 位置/链接/卡片 | 转成可读文本 | 不支持结构化发送，退化为文本 |

- 单个入站媒体上限 50MB；
- 普通 CDN 上传要求文件非空且 **小于 20MiB**；达到 20MiB 的视频/文件自动走
  BigCDN（`fileType` 22/35），图片没有大文件接口，超过上限会明确报错并提示压缩；
- 上传以文件句柄流式提交，不会把整个大文件读入内存；
- 语音编码需要 `ffmpeg` + `pilk`，任一缺失时出站语音降级为文件；
- 入站语音转文字依赖 QWPad 服务，最多等待 20 秒；成功时以文本代替音频附件，
  超时/失败只记 WARN，消息仍会带着语音附件进入 AstrBot；
- 入站媒体下载到临时目录，事件处理结束后由 AstrBot 清理。

### 9.5 引用回复

`quote_replies=true` 时，机器人对某条消息的首次回复会引用它（仅原消息为文本/富文本）。
引用失败会自动退化为普通文本，不会丢消息。

企业微信的引用发送要求 `sendHyperText` 带一个空文本段，插件已处理该兼容细节。

### 9.6 Markdown 与长消息

企业微信不渲染 Markdown，`strip_markdown=true` 会把粗体、斜体、标题、行内代码、
代码围栏、链接、图片语法转成可读纯文本。超过 `max_message_length` 的回复按换行
优先切分，再硬切超长段落；同一会话发送串行化，避免分片交叉。

### 9.7 流式输出

企业微信不支持编辑已发送消息，因此 `support_streaming_message=false`。AstrBot 的
流式输出会先聚合，再一次性发送。

### 9.8 主动消息

平台声明 `support_proactive_message=true`，AstrBot 可通过 `send_by_session()` 向已
知的会话主动发送消息。主动发送不依赖最近一条入站消息，也不会自动引用。

## 10. 多账号

**一个平台实例 = 一个 TOKEN = 一个企业微信账号。** 多账号就多加实例：

```text
平台实例 wecom(Pad)         (TOKEN A) ── 企业微信账号 A
平台实例 wecom(Pad)-shop2   (TOKEN B) ── 企业微信账号 B
```

每个实例独立：

- 轮询协程、游标、去重；
- 白名单/群策略、引用开关；
- 收发统计和状态接口记录；
- 运行时目录 `plugin_data/astrbot_plugin_qwpad/<安全化 id>/`。

注意事项：

- `id` 必须全平台唯一；
- 同一 TOKEN 不能配置到两个实例，否则后启动的实例会报错退出；
- 不支持同一账号多实例负载均衡或主备切换；
- 多实例共享 AstrBot 的 LLM/插件配置，但会话 UMO 中的平台 id 不同。

## 11. 状态接口

插件注册了 Web API（需要 Dashboard 登录态）：

```text
POST /api/plug/astrbot_plugin_qwpad/status
```

请求体可选 `{"platform_id": "wecom(Pad)"}`，不传则返回所有实例。

```bash
curl -s -X POST http://127.0.0.1:6185/api/plug/astrbot_plugin_qwpad/status \
  -H "Authorization: Bearer $ASTRBOT_DASHBOARD_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

响应示例：

```json
{
  "instances": [
    {
      "platform_id": "wecom(Pad)",
      "connected": true,
      "status": "running",
      "account": {
        "user_id": "zhangsan",
        "nickname": "张三",
        "corp_name": "示例企业",
        "online": true
      },
      "cursor": 12345,
      "dedupe_size": 42,
      "messages_in": 10,
      "messages_out": 12,
      "last_event_at": 1760000000.0,
      "policy": {
        "allow_users": ["zhangsan"],
        "allow_groups": ["roomid1"],
        "block_users": [],
        "block_groups": [],
        "allow_all_users": false,
        "group_reply": "atbot"
      },
      "last_error": null
    }
  ],
  "count": 1,
  "hint": "connected=false 时查看 last_error；TOKEN 不会通过接口返回。"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `connected` | 适配器是否处于运行状态 |
| `status` | AstrBot 平台状态（如 `running`） |
| `account` | 当前登录的企业微信账号信息；`online` 来自 `userOnlineStatus=2`，查询失败/缺字段时为 `null` |
| `cursor` | 已处理到的服务端事件 id |
| `dedupe_size` | 当前进程内去重集合大小（上限 2000） |
| `messages_in/out` | 本次运行累计收发条数 |
| `last_event_at` | 最近一条入站消息的 Unix 时间戳 |
| `policy` | 当前生效的白/黑名单与群模式 |
| `last_error` | 最近一次错误（无错误为 `null`） |

接口**不会返回 TOKEN**，也不会返回消息内容。

## 12. 数据与状态文件

插件状态写在 AstrBot 的插件数据目录，不写源码目录：

```text
<ASTRBOT_DATA>/plugin_data/astrbot_plugin_qwpad/
└── <safe_instance_id>/
    └── cursor.json        # {"lastEventId": 12345, "updatedAt": "..."}
```

`<safe_instance_id>` 由平台实例 `id` 安全化而来：保留字母、数字、`.`、`_`、`-`，
其他字符替换为 `_`。例如：

```text
wecom(Pad)        → wecom_Pad
wecom(Pad)-shop2  → wecom_Pad_-shop2
```

维护建议：

- 升级/重装插件前备份整个 `plugin_data/astrbot_plugin_qwpad/`；
- 不要在两个运行中的实例之间手工复制 `cursor.json`；
- 需要重新拉取积压消息时，先停用平台实例，删除对应 `cursor.json`，再启动；
  此时会重新应用 `startup_backlog_minutes`；
- 删除游标可能导致旧消息被重新处理，请确认不会造成重复回复。

## 13. 日志与排障

### 13.1 关键日志

```text
Loading plugin astrbot_plugin_qwpad ...
[QWPad:wecom(Pad)] 已连接 <昵称>（<企业> / <userId>），游标 <n>，策略 ...
[QWPad:wecom(Pad)] 收到私消息 room=... user=... type=... len=... media=...
[QWPad:wecom(Pad)] 收到群消息 room=... user=... type=... len=... media=...
[QWPad:wecom(Pad)] 已发送 <n> 条 -> <chat_id>
```

错误日志：

```text
[QWPad:...] TOKEN 无效或已失效: ...
[QWPad:...] 同一 TOKEN 已被平台实例 ... 使用
[QWPad:...] 拉取事件失败，<n>s 后重试: ...
[QWPad:...] 账号掉线: ...
```

### 13.2 常见问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 新增平台下拉没有 `wecom(Pad)` | 插件未加载 / 版本低于 0.2.0 / 未重启 | 看启动日志 `Loading plugin`，确认 metadata 版本，重启 AstrBot |
| 启动报 `TOKEN 未配置` | `token` 为空 | 在平台配置填入 `wxa_...` |
| 启动报 `同一 TOKEN 已被平台实例 ... 使用` | 同一 AstrBot 内重复实例，或 Hermes 等外部程序在轮询 | 只保留一个消费者；删除/停用重复实例 |
| 启动报 `TOKEN 无效或已失效` | TOKEN 错误、被重置、`api_base` 不对 | 在 QWPad 控制台重新复制 TOKEN，检查 `api_base` |
| 启动报 `账号未在线（userOnlineStatus=...）` | `errorCode=0` 只表示查询成功，`userOnlineStatus` 不是 2 就表示账号未在线 | 在 QWPad 控制台重新扫码登录，保持 pad 在线 |
| `connected=true` 但私聊不回复 | 白名单为空或发送者不在 `allow_users` | 看启动 WARN；把对方 userId 加入白名单，或临时 `allow_all_users=true` |
| 群里 @ 了也不回复 | 群不在 `allow_groups`、`group_reply=none`、没有真正 @、AstrBot 唤醒/LLM 配置问题 | 查日志「策略拒绝」原因；确认 mention；检查 AstrBot 的唤醒与模型配置 |
| 群里未 @ 不回复 | 默认 `atbot` 只放行 @ 消息 | 需要观察用 `all`；需要主动回复请配合主动回复插件 |
| 同一条消息回复两次 | 两个轮询者（Hermes + AstrBot，或两个 AstrBot 实例），或两个平台实例用了同一 TOKEN | 只保留一个消费者 |
| 重启后回复了旧消息 | `startup_backlog_minutes=0`、游标被删除、或游标写入失败 | 设回 5（或合适值）；检查数据目录写权限 |
| 图片/文件收不到 | 媒体链接过期、超过 50MB、QWPad 文件服务异常 | 看 `下载媒体异常` 日志；让对方重发 |
| 语音变成文件 | 缺少 `ffmpeg` 或 `pilk` | 安装依赖；确认容器内可执行 |
| 引用不显示或退化成普通消息 | 原消息不是文本/富文本，或引用接口失败 | 属预期降级；普通文本仍会送达 |
| 状态接口 401 | 没有 Dashboard 登录态 | 用 Dashboard 的 Bearer token，或直接看日志 |
| 修改配置不生效 | 平台实例未重载 | 在 Dashboard 重载平台或重启 AstrBot |
| 轮询频繁超时 | 网络到 `api_base` 不稳定，或 `poll_wait_seconds` 过大 | 检查网络；适当降低等待秒数 |

### 13.3 排查顺序

1. 插件是否加载（`Loading plugin`）；
2. 平台实例是否注册（`Loading IM platform adapter wecom(Pad)`）；
3. TOKEN/账号是否在线（`已连接` 或错误日志）；
4. 消息是否到达（`收到...消息`）；
5. 是否被策略拒绝（debug 日志里的 `策略拒绝`）；
6. AstrBot 是否唤醒/调用模型；
7. 出站是否成功（`已发送` 或发送错误）。

需要更详细日志时，把插件日志级别调到 DEBUG（AstrBot 插件日志配置）。

## 14. 架构

```text
企业微信
   │  pad 协议
   ▼
QWPad OpenAPI
   │  GET /events（long-poll，unified）
   ▼
qwpad_client ──► qwpad_messages（解析/过滤）
                      │
                 qwpad_policy（准入）
                      │
              AstrBotMessage + QWPadMessageEvent
                      │
                AstrBot 事件总线
                      │
      qwpad_send ◄── MessageChain（LLM / 插件）
          │
   /msg/* + /cloud/cdnBigUpload
          │
       企业微信
```

| 文件 | 职责 |
| --- | --- |
| `qwpad_adapter.py` | 平台生命周期、轮询、事件分发、发送入口、统计 |
| `qwpad_client.py` | QWPad HTTP 客户端（事件、发送、上传、下载、语音转文字、资料） |
| `qwpad_messages.py` | 入站事件解析：文本/富文本/卡片/位置/合并转发/引用/媒体 |
| `qwpad_media.py` | 媒体下载、临时文件、SILK 编解码 |
| `qwpad_policy.py` | 白/黑名单与群回复策略 |
| `qwpad_send.py` | `MessageChain` → QWPad：分片、Markdown 清洗、@、引用、媒体 |
| `qwpad_event.py` | 消息事件；流式输出聚合；出站计数 |
| `qwpad_storage.py` | 游标持久化、内存去重、原子写、实例目录 |
| `main.py` | 插件入口与状态 Web API |

运行细节：

- 每 300 秒检查一次账号在线状态（只认 `userOnlineStatus=2`），掉线只记录警告，不停止轮询；
- 轮询异常按 1s→60s 退避；TOKEN 失效会停止该实例；
- 去重集合为进程内 LRU，容量 2000；
- 群名/联系人昵称缓存 15 分钟；
- 引用锚点最多保留 500 条，待引用会话最多 200 个。

## 15. 升级、迁移与回滚

### 15.1 常规升级

1. 备份 `cmd_config.json`、数据库和 `plugin_data/astrbot_plugin_qwpad/`；
2. 替换插件目录或从仓库更新；
3. 重启 AstrBot；
4. 确认日志版本号、`wecom(Pad)` 注册、账号在线。

### 15.2 从 0.1.x 升级到 0.2.x

0.2.0 把适配器注册名、平台 `type`、默认 `id` 从 `qwpad` 统一为 `wecom(Pad)`。
升级前必须把已有平台配置改成新值，否则 AstrBot 找不到对应适配器：

```json
{
  "type": "wecom(Pad)",
  "id": "wecom(Pad)"
}
```

如果 `id` 也改了：

- AstrBot 的 UMO（`平台id:消息类型:会话id`）会变化，历史会话/偏好/统计需要迁移；
- 运行时目录会从旧 id 的安全化名称变为新名称，需要把 `cursor.json` 移到新目录
  （或接受重新拉取）；
- 多账号实例请逐个确认 `id` 唯一。

生产迁移前请先停 AstrBot，备份 `cmd_config.json`、数据库和插件数据目录。

### 15.3 从 0.2.x 升级到 0.3.0

0.3.0 不修改平台注册名、配置字段或数据格式，可直接覆盖插件目录后重启。该版本
主要做健壮性加固：

- 游标读写、临时文件、语音/媒体落盘等阻塞 I/O 全部移入线程池，避免卡住事件循环；
- TOKEN 失效时 `run()` 重新抛出错误，平台状态正确变为 `ERROR`（不再停在 `RUNNING`）；
- 流式输出只发送一次、只上报一次 `msg_event_tick`，修复重复指标；
- 补齐 `requirements.txt`，移除 `metadata.yaml` 中非标准字段。

### 15.4 回滚

1. 停 AstrBot；
2. 恢复旧插件目录、`cmd_config.json`、数据库备份和插件数据目录；
3. 启动并确认旧版本日志正常。

## 16. 安全建议

- `token` 是账号凭据：只保存在 AstrBot 配置中，不要提交到 Git、不要发群、不要截图；
- 本插件不会通过状态接口返回 TOKEN，日志只使用 TOKEN 指纹；
- 下载 `cloudUrl` 使用独立的无鉴权 HTTP 客户端，不会把 TOKEN 发给对象存储；`fs://`
  地址说明存储未开启 HTTP 出口，插件会明确报错而不是拼接凭据；
- 默认拒绝所有消息，先配置白名单再放开；
- 生产环境使用 HTTPS 的 `api_base`；
- 一个 TOKEN 只允许一个消费者，避免 Hermes/AstrBot 同时轮询；
- 限制 Dashboard 访问权限，状态接口依赖 Dashboard 登录态；
- 定期轮换 TOKEN，轮换后同步更新平台配置并重启实例；
- 不要把 `plugin_data` 目录、`cmd_config.json` 或日志原样公开。

## 17. 已知限制

- **无消息编辑能力**：流式输出只能聚合后一次性发送；
- **引用有类型限制**：仅文本/富文本原消息可引用；失败自动退化为普通文本；
- **语音限制**：出站 SILK 目标 ≤60KB，超限降码率；`ffmpeg`/`pilk` 缺失时降级为文件；
  入站转文字依赖 QWPad 服务，失败时只能把原始语音作为附件；
- **媒体大小**：入站单文件上限 50MB；出站普通 CDN 上传必须小于 20MiB，
  达到 20MiB 的视频/文件自动改走 BigCDN，图片没有大文件接口只能压缩后再发；
- **@ 必须是真实 mention**：纯文本里的 `@名字` 不算；
- **群成员白名单是「或」关系**：`allow_users` 中的成员可在任意群通过准入（`atbot` 仍需 @）；
- **同一 TOKEN 不支持多实例**：没有负载均衡/主备；
- **去重只在进程内**：正常重启依靠游标不重放；异常崩溃时可能重复处理极少量边界消息；
- **Markdown 清洗是启发式**：复杂嵌套格式可能残留符号；
- **依赖 QWPad 服务**：QWPad 不可用、账号掉线或 TOKEN 失效时无法收发；
- **不提供 Webhook 模式**：仅长轮询，适合无公网入站的部署。

## 18. 开发与测试

测试需要 AstrBot 运行环境（依赖 `astrbot` 包）。推荐在 AstrBot 容器内跑：

```bash
# 把插件放到容器可访问的位置，目录名保持 astrbot_plugin_qwpad
docker exec -w /AstrBot/data/plugins astrbot \
  python -m pytest -q astrbot_plugin_qwpad/tests
```

本地已安装 AstrBot 依赖时也可以：

```bash
python -m pytest -q
```

测试覆盖：

- 事件解析：文本/富文本/卡片/位置/合并转发/引用/媒体；
- 策略：白/黑名单、群模式、默认拒绝；
- 发送：分片、Markdown 清洗、@、引用、媒体降级；
- 适配器契约：元数据、消息构建、统计、重复 TOKEN 锁；
- 生命周期：TOKEN 失效时 `run()` 抛错并置 `ERROR`；
- 流式输出：多个分片聚合为一次发送，不重复调用基类 `send_streaming`；
- 状态接口字段。

> 单元测试全绿不代表主路径能跑。改完适配器后至少做一次真实主路径验证：
> 启动日志出现 `已连接`、状态接口 `connected=true`、发一条真实私聊/群 @ 消息并确认送达。

代码风格：Python 3.10+，使用类型标注；平台适配器遵循 AstrBot
`Platform` 契约（`run/terminate/meta/create_event/send_by_session/get_stats`）。

## 19. FAQ

**Q：需要公网 IP 或回调地址吗？**  
不需要。插件主动长轮询 QWPad，只需要能出网访问 `api_base`。

**Q：一个 AstrBot 能接多个企业微信账号吗？**  
可以。添加多个 `wecom(Pad)` 平台实例，每个实例一个 TOKEN、一个唯一 `id`。

**Q：Hermes 和 AstrBot 能同时用同一个 TOKEN 吗？**  
不能。一个 TOKEN 只能有一个轮询者，否则会抢事件、重复回复、游标互相覆盖。

**Q：群消息不 @ 能回复吗？**  
`group_reply=all` 可以让未 @ 消息进入事件流，但不会强制唤醒 LLM；需要主动回复时
请配合 AstrBot 的主动回复插件，并控制频率。

**Q：怎么获取 userId / roomId？**  
看插件日志里的 `user=` / `room=` 字段，或使用 QWPad 控制台的会话信息。配置里填 ID，
不要填昵称或群名。

**Q：重启后会回复历史消息吗？**  
正常不会。游标持久化在 `cursor.json`；首次启动且无游标时，只处理最近
`startup_backlog_minutes` 分钟。把该值设为 `0` 会处理服务端全部积压，谨慎使用。

**Q：为什么回复被拆成多条？**  
超过 `max_message_length`（默认 2000 字符）会自动分片；可按需调大，但企业微信单条
消息有长度限制，不建议设得过大。

**Q：支持发送 Markdown 吗？**  
企业微信不渲染 Markdown，插件默认清洗成纯文本。需要保留原文时可关闭
`strip_markdown`，但显示效果由客户端决定。

**Q：TOKEN 会出现在日志或接口里吗？**  
不会。日志只用 TOKEN 指纹，状态接口不返回 TOKEN。

---

相关文档：[部署与验证](DEPLOY.md) · [更新日志](CHANGELOG.md)
