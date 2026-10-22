# 部署与验证

本文面向运维/部署人员，说明如何把 `astrbot-plugin-qwpad` 装进 AstrBot、配置平台
实例、验证收发，以及从 0.1.x 迁移到 0.2.x。

- 插件版本：`0.2.0`
- 验证环境：AstrBot `4.29.0`（Docker，`soulter/astrbot:v4.29.0`）
- 仓库：<https://github.com/jiexiaoyin/Astebot-plugin-qwpad>

> 文中 `ASTRBOT_DATA` 指 AstrBot 的 data 目录。Docker 部署通常是宿主机挂载目录，
> 例如 `/opt/astrbot/data`；非 Docker 部署通常是 AstrBot 根目录下的 `data/`。
> 容器内路径一般为 `/AstrBot/data`。

---

## 0. 前置检查

### 0.1 一个 TOKEN 只能有一个消费者

```bash
# 确认 AstrBot 状态
docker ps --filter name=astrbot --format '{{.Names}} {{.Status}}'

# 确认没有其他程序在轮询同一个 QWPad TOKEN
# 例如 Hermes 的 qwpad 插件、旧版脚本、另一个 AstrBot 实例
```

同一 TOKEN 被两个程序轮询会导致：事件被抢消费、游标互相覆盖、消息丢失或重复回复。
启用本插件前先停用其他消费者。

### 0.2 确认账号在线

在 QWPad 控制台确认目标企业微信账号已扫码登录且在线。账号掉线时插件会报：

```text
QWPad 账号不在线（errorCode=...），请先在 QWPad 控制台完成扫码登录
```

### 0.3 确认网络

AstrBot 所在容器/主机必须能访问 `api_base`（默认 `https://api.qwpad.com`）。
插件是出站长轮询，**不需要公网入站端口或回调地址**。

```bash
# 在 AstrBot 容器内执行
docker exec astrbot sh -c 'curl -sS -o /dev/null -w "%{http_code}\n" https://api.qwpad.com'
```

返回 2xx/401/403 都说明网络可达（401/403 只是没带 TOKEN）。

---

## 1. 安装插件

### 方式 A：Dashboard 安装（推荐）

AstrBot Dashboard → 插件管理 → 安装插件 → 填入仓库地址：

```text
https://github.com/jiexiaoyin/Astebot-plugin-qwpad
```

安装后确认插件版本为 `0.2.0`。

### 方式 B：手动部署

```bash
rsync -a --delete \
  --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  /path/to/astrbot-plugin-qwpad/ \
  "$ASTRBOT_DATA/plugins/astrbot-plugin-qwpad/"

docker restart astrbot
```

插件目录名保持 `astrbot-plugin-qwpad`。

> 升级时不要删除 `$ASTRBOT_DATA/plugin_data/astrbot-plugin-qwpad/`，否则游标和
> 运行状态会丢失。

---

## 2. 检查运行依赖

```bash
docker exec astrbot sh -c '
  echo -n "ffmpeg: "; command -v ffmpeg || echo MISSING
  echo -n "ffprobe: "; command -v ffprobe || echo MISSING
  python -c "import httpx, pilk; print(\"httpx/pilk: ok\")"
'
```

- `httpx`、`pilk`：AstrBot 镜像通常已自带；
- `ffmpeg`/`ffprobe`：用于出站语音转 SILK、视频时长探测。缺失时插件仍可运行，
  但语音会降级为普通文件。

在 Debian/Ubuntu 基础镜像中安装：

```bash
docker exec -u root astrbot sh -c 'apt-get update && apt-get install -y ffmpeg'
```

（容器重建后需要重新安装；生产环境建议构建自定义镜像或使用宿主机挂载的静态
`ffmpeg`。）

---

## 3. 配置平台实例

### 3.1 Dashboard 配置

平台配置 → 新增 → **wecom(Pad)**，按下表填写：

| 字段 | 必填 | 示例 | 说明 |
| --- | --- | --- | --- |
| `id` | 是 | `wecom(Pad)` | 平台实例唯一标识；多账号时必须不同 |
| `token` | 是 | `wxa_xxx` | 该账号的 QWPad TOKEN |
| `api_base` | 否 | `https://api.qwpad.com` | 私有化部署时修改 |
| `allow_users` | 否 | `zhangsan,lisi` | 允许私聊的成员 userId |
| `allow_groups` | 否 | `roomid1,roomid2` | 允许进入的群 roomId |
| `group_reply` | 否 | `atbot` | `atbot` / `all` / `none` |
| `quote_replies` | 否 | `true` | 回复时引用原消息 |
| `strip_markdown` | 否 | `true` | 发送前清洗 Markdown |
| `poll_wait_seconds` | 否 | `25` | 长轮询等待秒数，1–30 |
| `poll_limit` | 否 | `50` | 单次拉取上限，1–500 |
| `max_message_length` | 否 | `2000` | 文本分片长度 |
| `startup_backlog_minutes` | 否 | `5` | 首次启动回看分钟数；`0`=全部积压 |

保存并启用。**白名单为空且 `allow_all_users=false` 时所有消息都会被拒绝**，
启动日志会打印 WARN；这是安全默认值，不是故障。

### 3.2 直接编辑配置文件（可选）

如果必须手改 `cmd_config.json`：

```json
{
  "type": "wecom(Pad)",
  "id": "wecom(Pad)",
  "enable": true,
  "token": "wxa_xxx",
  "api_base": "https://api.qwpad.com",
  "allow_users": "",
  "allow_groups": "",
  "block_users": "",
  "block_groups": "",
  "allow_all_users": false,
  "group_reply": "atbot",
  "quote_replies": true,
  "strip_markdown": true,
  "poll_wait_seconds": 25,
  "poll_limit": 50,
  "max_message_length": 2000,
  "startup_backlog_minutes": 5
}
```

注意：

- `type` 必须是 `wecom(Pad)`，AstrBot 用它选择适配器；
- 文件可能是 UTF-8 **带 BOM**，修改时保留 BOM；
- 改动前备份：

```bash
cp "$ASTRBOT_DATA/cmd_config.json" \
   "/data/astrbot-cmd_config-$(date +%Y%m%d-%H%M%S).json"
```

---

## 4. 启动与日志

```bash
docker restart astrbot
docker logs --since 3m astrbot 2>&1 | grep -iE 'qwpad|wecom\(Pad\)|platform'
```

期望看到：

```text
Loading plugin astrbot-plugin-qwpad ...
Plugin astrbot-plugin-qwpad (0.2.0) ...
Loading IM platform adapter wecom(Pad)(wecom(Pad)) ...
[QWPad:wecom(Pad)] 已连接 <昵称>（<企业> / <userId>），游标 <n>，策略 ...
```

首次启动且白名单为空时，额外出现：

```text
[QWPad:wecom(Pad)] 白名单为空且 allow_all_users=false —— 所有消息都会被拒绝。
```

配置白名单后重启/重载平台即可消除。

---

## 5. 验证

### 5.1 状态接口

```bash
curl -s -X POST http://127.0.0.1:6185/api/plug/astrbot-plugin-qwpad/status \
  -H "Authorization: Bearer $ASTRBOT_DASHBOARD_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"platform_id": "wecom(Pad)"}'
```

期望：

- `connected=true`
- `status="running"`
- `account.user_id` 正确
- `last_error=null`

字段详情见 [README 状态接口](README.md#11-状态接口)。

### 5.2 私聊验证

用 `allow_users` 内的成员私聊机器人，日志应出现：

```text
[QWPad:wecom(Pad)] 收到私消息 room=... user=... type=... len=... media=0
[QWPad:wecom(Pad)] 已发送 1 条 -> ...
```

### 5.3 群聊验证

在 `allow_groups` 内的群 @ 机器人。日志应出现：

```text
[QWPad:wecom(Pad)] 收到群消息 room=... user=... type=... len=... media=0
```

未 @ 时不出现该日志（`atbot` 模式），属于预期。

### 5.4 媒体验证（可选）

- 私聊发图片/文件/语音，确认日志 `media=1` 且模型能收到；
- 让机器人回复语音，确认收到语音气泡而不是文件；
- 语音失败时检查 `ffmpeg`/`pilk`。

### 5.5 重启不重放

重启 AstrBot 后观察日志：游标应从上次位置继续，不应批量回复历史消息。
如果出现旧消息回复，检查 `startup_backlog_minutes` 与 `cursor.json` 是否被删除。

---

## 6. 多账号部署

**一个平台实例 = 一个 TOKEN = 一个企业微信账号。** 多账号时重复第 3 节：

```text
平台实例 wecom(Pad)         (TOKEN A) ── 企业微信账号 A
平台实例 wecom(Pad)-shop2   (TOKEN B) ── 企业微信账号 B
```

要点：

- 每个实例 `id` 唯一；
- 每个实例独立游标目录 `plugin_data/astrbot-plugin-qwpad/<安全化 id>/`；
- 同一 TOKEN 不能配到两个实例，否则后启动者报
  `同一 TOKEN 已被平台实例 ... 使用`；
- 不支持同一账号多实例负载均衡/主备。

---

## 7. 从 0.1.x 升级到 0.2.x

0.2.0 把适配器注册名、平台 `type`、默认 `id` 从 `qwpad` 统一为 `wecom(Pad)`。
AstrBot 4.29 的机器人列表/新增下拉直接显示注册名和配置 `id`，所以三处必须一致。

### 7.1 备份

先停 AstrBot，避免运行中的实例把旧配置写回：

```bash
docker stop astrbot

cp "$ASTRBOT_DATA/cmd_config.json" \
   "/data/astrbot-cmd_config-$(date +%Y%m%d-%H%M%S).json"
cp "$ASTRBOT_DATA/data_v4.db" \
   "/data/astrbot-data_v4-$(date +%Y%m%d-%H%M%S).db"
cp -a "$ASTRBOT_DATA/plugin_data/astrbot-plugin-qwpad" \
   "/data/astrbot-qwpad-plugin-data-$(date +%Y%m%d-%H%M%S)"
```

数据库文件名按实际版本可能不同（如 `data_v4.db`）。

### 7.2 修改平台配置

把已有平台对象的 `type`/`id` 从 `qwpad` 改为 `wecom(Pad)`：

```json
{
  "type": "wecom(Pad)",
  "id": "wecom(Pad)"
}
```

多账号实例逐个改，保持 `id` 唯一。

### 7.3 迁移 UMO（仅当 id 改变）

AstrBot 的 UMO 形如 `平台id:消息类型:会话id`。改了 `id` 后，历史会话、偏好、统计
会指向旧 UMO。需要迁移时，先备份数据库，再在事务中更新这些表的平台前缀：

- `platform_stats.platform_id`
- `provider_stats.umo`
- `conversations.platform_id` / `conversations.user_id`
- `preferences.scope_id` / `preferences.value`（如 `umop_config_routing`）
- `umo_aliases.umo`
- 会话内容中回显的 UMO 前缀

示例（SQLite，表结构随 AstrBot 版本可能变化，执行前先 `PRAGMA table_info` 核对）：

```sql
UPDATE platform_stats SET platform_id='wecom(Pad)' WHERE platform_id='qwpad';
UPDATE provider_stats SET umo='wecom(Pad):' || substr(umo, 6) WHERE umo LIKE 'qwpad:%';
UPDATE conversations SET platform_id='wecom(Pad)' WHERE platform_id='qwpad';
UPDATE conversations SET user_id='wecom(Pad):' || substr(user_id, 6) WHERE user_id LIKE 'qwpad:%';
UPDATE preferences SET scope_id='wecom(Pad):' || substr(scope_id, 6) WHERE scope_id LIKE 'qwpad:%';
UPDATE umo_aliases SET umo='wecom(Pad):' || substr(umo, 6) WHERE umo LIKE 'qwpad:%';
```

> `qwpad:` 是 6 个字符（`q w p a d :`），所以 `substr(..., 6)` 从冒号后的第一个
> 字符开始；如果 SQL 里已经拼了冒号，注意不要重复。执行后抽样核对。

如果不需要保留历史会话，可以跳过 UMO 迁移。

### 7.4 迁移运行时目录

实例目录名由 `id` 安全化得到（保留字母/数字/`.`/`_`/`-`，其他字符替换为 `_`）：

```text
qwpad        → qwpad
wecom(Pad)   → wecom_Pad
```

改了 `id` 后，把旧目录里的 `cursor.json` 移到新目录，或接受重新拉取：

```bash
cd "$ASTRBOT_DATA/plugin_data/astrbot-plugin-qwpad"
mkdir -p wecom_Pad
[ -f qwpad/cursor.json ] && mv qwpad/cursor.json wecom_Pad/cursor.json
```

不要在两个实例之间复制游标。

### 7.5 部署并启动

```bash
# 替换插件代码
rsync -a --delete \
  --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  --exclude='.pytest_cache' \
  /path/to/astrbot-plugin-qwpad/ \
  "$ASTRBOT_DATA/plugins/astrbot-plugin-qwpad/"

docker start astrbot
docker logs --since 3m astrbot 2>&1 | grep -iE 'qwpad|wecom\(Pad\)'
```

确认：

```text
Plugin astrbot-plugin-qwpad (0.2.0) ...
Loading IM platform adapter wecom(Pad)(wecom(Pad)) ...
[QWPad:wecom(Pad)] 已连接 ...
```

---

## 8. 回滚

1. 停 AstrBot；
2. 恢复旧插件目录；
3. 恢复 `cmd_config.json`、数据库和 `plugin_data/astrbot-plugin-qwpad/` 备份；
4. 启动，确认旧版本日志正常。

```bash
docker stop astrbot
cp /data/astrbot-cmd_config-<ts>.json "$ASTRBOT_DATA/cmd_config.json"
cp /data/astrbot-data_v4-<ts>.db "$ASTRBOT_DATA/data_v4.db"
# 恢复插件目录与 plugin_data 后：
docker start astrbot
```

---

## 9. 安全基线

- TOKEN 只放 AstrBot 配置，不写脚本、不进 Git、不发群；
- 默认拒绝：先配置白名单再放开流量；
- 一个 TOKEN 只允许一个消费者（Hermes / AstrBot / 其他脚本）；
- 生产使用 HTTPS `api_base`；
- 限制 Dashboard 访问，状态接口依赖登录态；
- 不在公开渠道粘贴 `cmd_config.json`、`plugin_data` 或完整日志；
- 轮换 TOKEN 后同步更新平台配置并重启实例。

---

## 10. 快速排障

| 现象 | 检查 |
| --- | --- |
| 新增下拉没有 `wecom(Pad)` | 插件是否加载、版本是否 0.2.0、是否重启 |
| `TOKEN 未配置` | 平台配置 `token` 是否为空 |
| `同一 TOKEN 已被平台实例 ... 使用` | 是否有重复实例或外部轮询者 |
| `TOKEN 无效或已失效` | TOKEN 是否正确、`api_base` 是否正确 |
| 账号不在线 | QWPad 控制台重新扫码登录 |
| 连接成功但不回复 | 白名单、群 @、AstrBot 唤醒/LLM 配置 |
| 回复重复 | 两个轮询者或两个实例共用 TOKEN |
| 语音变文件 | `ffmpeg` / `pilk` 是否可用 |
| 重启回复旧消息 | `startup_backlog_minutes`、`cursor.json` |
| 状态接口 401 | 是否带 Dashboard 登录 token |

更多细节见 [README 日志与排障](README.md#13-日志与排障)。
