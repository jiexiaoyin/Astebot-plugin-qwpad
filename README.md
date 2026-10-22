# astrbot-plugin-qwpad

QWPad 企业微信 **pad 协议** 平台适配器，让 AstrBot 以企业微信账号身份收发消息。

> ⚠️ **一个 QWPad TOKEN 对应一个企业微信账号**，同一 TOKEN 同时只能运行一个轮询实例。
> 启用本插件前请确认 Hermes / 其他程序里的 qwpad 插件已停用，否则会重复消费事件。

## 功能

- 长轮询 `GET /events` 接收私聊、群聊；自动过滤系统/非聊天事件
- 文本、图片、语音、视频、文件双向收发
- 群聊 @ 唤醒；引用回复（`reply` 锚点）
- 入站语音 SILK → WAV，出站任意音频 → SILK 语音气泡（超 60KB 自动降码率）
- 白名单 / 黑名单 / 群回复策略；跨重启游标 + 去重
- 插件 Web API 查询连接状态（不返回 TOKEN）

## 环境要求

- AstrBot `>= 4.16.0, < 5`（在 4.29.0 上验证）
- Python 3.10+
- 系统 `ffmpeg`（出站语音转码；缺失时语音降级为文件）
- Python 包 `pilk`（SILK 编解码，AstrBot 镜像已自带）

## 安装

### 方式一：从仓库安装

AstrBot Dashboard → 插件管理 → 安装插件 → 填入仓库地址。

### 方式二：手动部署

```bash
rsync -a --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  ./ /path/to/astrbot/data/plugins/astrbot-plugin-qwpad/
# 重启 AstrBot
```

插件目录名必须保持 `astrbot-plugin-qwpad`。

## 配置

AstrBot Dashboard → 平台配置 → 新增「wecom(Pad)」。字段说明：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `token` | — | QWPad 业务 TOKEN（`wxa_` 前缀）；一个 TOKEN 对应一个企业微信账号 |
| `api_base` | `https://api.qwpad.com` | 私有化部署时改这里 |
| `allow_users` | 空 | 私聊/群成员白名单，userId 逗号分隔 |
| `allow_groups` | 空 | 群白名单，roomId 逗号分隔 |
| `block_users` | 空 | 用户黑名单，优先级高于白名单 |
| `block_groups` | 空 | 群黑名单，优先级高于白名单 |
| `allow_all_users` | `false` | 放开所有聊天（仅调试/内部账号） |
| `group_reply` | `atbot` | `atbot`=只放行 @ 我的群消息；`all`=全部群消息进入事件流；`none`=忽略群消息 |
| `quote_replies` | `true` | 回复时引用原消息（仅文本类原消息可引用） |
| `strip_markdown` | `true` | 发送前清洗 Markdown（企业微信不渲染 Markdown） |
| `poll_wait_seconds` | `25` | long-poll 等待秒数，范围 1–30 |
| `poll_limit` | `50` | 单次拉取条数，范围 1–500 |
| `max_message_length` | `2000` | 单条文本上限，超长自动分片 |
| `startup_backlog_minutes` | `5` | 首次启动（无游标）只处理最近 N 分钟，`0`=处理全部积压 |

### 多账号

**一个平台实例 = 一个 TOKEN = 一个企业微信账号。** 多账号请添加多个平台实例：

Dashboard → 平台配置 → 再新增一个「wecom(Pad)」，`id` 唯一（如 `wecom(Pad)-shop2`），
`token` 填另一个账号的 TOKEN。每个实例独立轮询、游标、白名单/群策略和收发统计。

```text
平台实例 wecom(Pad)         (TOKEN A) ── 账号 A
平台实例 wecom(Pad)-shop2   (TOKEN B) ── 账号 B
```

**同一个 TOKEN 只允许一个轮询者**：重复配置会在启动时被拒绝，避免两个实例抢同一
账号的事件和游标。TOKEN 不能复用，也不存在「一个 TOKEN 下挂多个账号」的配置。

### 准入 ≠ 唤醒

- **私聊**：白名单内直接进入 AstrBot 并唤醒。
- **群聊**：`group_reply=atbot` 时，只有 @ 了机器人账号的消息才进入；`all` 时全部进入，但未 @ 的消息按 AstrBot 默认规则不会唤醒 LLM（可用于上下文/统计）。
- **默认拒绝**：白名单为空且 `allow_all_users=false` 时，所有消息都会被丢弃，仅保持连接。这是安全的初始状态。

## 架构

```
企业微信 ── QWPad /events ──▶ qwpad_messages 解析
                                  │
                         策略 classify（准入）
                                  │
                     AstrBotMessage / QWPadMessageEvent
                                  │
                            AstrBot 事件总线
                                  │
      qwpad_send ◀── MessageChain ── LLM / 插件
          │
      QWPad /msg/*、/cloud/cdnBigUpload
          │
       企业微信
```

| 文件 | 职责 |
| --- | --- |
| `qwpad_client.py` | QWPad HTTP 客户端（事件、发送、上传、下载、资料） |
| `qwpad_messages.py` | 入站事件解析（富文本、卡片、位置、合并转发、引用、媒体） |
| `qwpad_media.py` | 媒体下载与 SILK 编解码 |
| `qwpad_policy.py` | 白/黑名单与群回复策略 |
| `qwpad_send.py` | MessageChain → QWPad 发送（分片、@、引用、媒体） |
| `qwpad_event.py` | 事件对象；流式输出聚合为单条发送 |
| `qwpad_storage.py` | 游标、去重集合、原子写 |
| `qwpad_adapter.py` | 平台生命周期、轮询、事件分发、统计 |
| `main.py` | 插件入口与状态 Web API |

## 状态接口

`POST /api/plug/astrbot-plugin-qwpad/status`（需要 Dashboard 登录态）

请求体可选 `{"platform_id": "wecom(Pad)"}`。返回在线状态、账号、游标、收发计数、当前策略与最近错误；
多平台实例时每个实例一条记录；**不会返回 TOKEN**。

## 媒体说明

| 类型 | 入站 | 出站 |
| --- | --- | --- |
| 图片 | CDN 下载，保留原格式 | 上传后 `sendImage` |
| 语音 | SILK → WAV（pilk） | ffmpeg → PCM 24k → SILK，≤60KB |
| 视频 | CDN 下载 | 上传 + ffprobe 时长 + `sendVideo` |
| 文件 | CDN 下载 | 上传后 `sendFile` |

入站文件落在 AstrBot 临时目录，事件结束由 `track_temporary_local_file()` 清理。

## 已知限制

- 企业微信没有消息编辑能力：`support_streaming_message=False`，流式输出会先聚合再一次性发送。
- 引用发送：QWPad 要求 `sendHyperText` 带一个空文本段，否则只显示引用不显示正文；插件已处理。
- 引用仅对文本/富文本原消息生效；发送失败自动退回普通文本。
- 一个 TOKEN 只能对应一个平台实例；多账号请添加多个平台实例（见「多账号」）。
  同一账号不支持多实例负载均衡或主备切换。

## 开发与测试

```bash
python -m pytest -q
```

测试覆盖事件解析、策略、发送分片/@/引用、适配器消息构建与统计契约。

## 许可

MIT
