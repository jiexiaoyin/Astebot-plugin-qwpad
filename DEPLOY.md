# 部署与验证

以本机 AstrBot（Docker，`soulter/astrbot:v4.29.0`）为例。

## 0. 前置检查

```bash
# 确认没有其他程序在消费同一个 QWPad TOKEN
# （Hermes 的 qwpad 插件必须停用）
docker ps --filter name=astrbot --format '{{.Names}} {{.Status}}'
```

一个 TOKEN 只能有一个客户端实例；重复运行会导致事件被两边抢消费、游标互相覆盖。

## 1. 部署文件

```bash
rsync -a --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  /root/dev/astrbot-plugin-qwpad/ \
  /opt/1panel/apps/astrbot/astrbot/data/plugins/astrbot-plugin-qwpad/
```

## 2. 配置平台实例

编辑 `data/cmd_config.json`（**保留 UTF-8 BOM**），在 `platform` 数组追加：

```json
{
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
  "startup_backlog_minutes": 5,
  "type": "qwpad",
  "enable": true,
  "id": "qwpad"
}
```

改动前备份：

```bash
cp data/cmd_config.json /data/astrbot-cmd_config-$(date +%Y%m%d-%H%M%S).json
```

## 3. 重启并检查日志

```bash
docker restart astrbot
docker logs --since 3m astrbot 2>&1 | grep -iE 'qwpad|platform'
```

期望看到：

```
Loading plugin astrbot-plugin-qwpad ...
Loading IM platform adapter qwpad(qwpad) ...
[QWPad:qwpad] 已连接 <昵称>（<企业> / <userId>），游标 <n>，策略 ...
```

首次启动且白名单为空时会有一条「所有消息都会被拒绝」的 WARN —— 这是安全默认值，不是故障。

## 4. 验证状态接口

```bash
curl -s -X POST http://127.0.0.1:6185/api/plug/astrbot-plugin-qwpad/status \
  -H "Authorization: Bearer $ASTRBOT_DASHBOARD_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

期望 `connected=true`、`status=running`、`account.user_id` 正确、`last_error=null`。

## 5. 放开流量

按需配置以下任一项，然后重启或热重载平台：

- 私聊：`allow_users` 填入对方 userId（逗号分隔）
- 群聊：`allow_groups` 填入 roomId，并按需选择 `group_reply`
- 仅内部调试：`allow_all_users: true`（不建议长期开启）

## 回滚

```bash
cp /data/astrbot-cmd_config-<ts>.json data/cmd_config.json
docker restart astrbot
# 不需要时删除插件目录：
# rm -rf data/plugins/astrbot-plugin-qwpad
```
