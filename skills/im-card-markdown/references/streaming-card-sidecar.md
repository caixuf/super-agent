# 第三方流式卡片 sidecar：怎么问它自己

适用：卡片投递被一个**外部集成**接管——它有自己的 CLI、自己的 yaml、自己的 systemd 单元，
通过 hook 接过宿主的流式投递。这类集成不会把真相写进宿主日志（它只在状态**变化**时打一行），
所以别去 gateway 日志里找它。

## 探测顺序

```bash
C=~/.hermes/tools/<dist>-<ver>/bin/<cli-name>

# 1) 它自己的计数——先看这个，一步分开「没发」和「发了但空」
$C status --config <cfg.yaml>

# 2) 逐项解释（锚点、兼容性、运行时导入、SDK）
$C doctor --config <cfg.yaml> --hermes-dir <agent-dir> --hermes-home <home> --explain
```

`doctor` 的必填参数不是可选的：不给 `--config` 直接报 usage，别把它当成命令坏了。
`--explain` 给人类可读版，`--json` 给可解析版，两者互斥。

```bash
# 3) 宿主侧同一时刻的日志（证明内容确实产出了）
awk '$0 >= "<HH:MM:SS>" && $0 <= "<HH:MM:SS>"' <home>/logs/gateway.log | grep -vE '^Traceback|^  File'

# 4) IM 平台侧实际落了什么
curl -sS ".../im/v1/messages?container_id_type=chat&container_id=<chat>&page_size=50&sort_type=ByCreateTimeDesc" \
  -H "Authorization: Bearer $TOK"

# 5) sidecar 单元日志（注意：它只打状态变化，沉默不是健康）
journalctl --user -u <sidecar>.service --since "<t0>" --until "<t1>" --no-pager
```

## 计数怎么读

| 计数组合 | 说明什么 |
| :--- | :--- |
| `events_received` 高、`send_attempts` 低 | 事件进来了但大多没有触发发送（更新/忽略/去重） |
| `send_successes == send_attempts`、`failures = 0` | **卡片对象创建成功**，与内容是否为空无关 |
| `update_attempts` 远高于 `send_attempts` | 一卡多帧，符合流式原地更新；每帧都「成功」不等于每帧有正文 |
| `events_rejected` / `event_auth_rejections` > 0 | 事件被拒，投递链路的**上游**断了 |
| `events_received` > `events_applied` 且 `rejected = 0` | 有事件被静默丢弃或跳过（去重/超时/容量），差额本身就是待查项；别当舍入误差扫过去 |
| `integrity.status: ready` | 运行时归属校验通过；这是先决条件，不是投递证据 |
| `native_handoff.pending` 与 `acked` 同时为 0、`records` > 0 | 交接记录存在但两个终态计数都是 0，含义未定——**不要据此下结论**，去源码读这个计数何时自增 |

`bots list` 显示 `default` bot 存在时，`route_reason: bots.default` 配一条 `route_fallback` finding
是**噪声**，不影响投递，别把它当根因。

## 三个必须分清的假绿

1. **进程活着 ≠ 功能在线。** `systemctl is-active` → active、`NRestarts=0`，只能说明进程没退出。
   集成在「已启动但判定需要人工复核」时会静默不发卡片，状态字符串看 `integrity.status`，
   不看 `is-active`。
2. **零失败 ≠ 内容送达。** 全部 `success` 的卡片可以是空卡——用户看到的就是一张占位图。
   这时去查首帧内容与更新帧正文，不要再怀疑投递。
3. **读回 body ≠ 渲染证据。** 2.0 卡通过 API 读回恒为「请升级客户端」占位符（见 SKILL.md 第一节），
   拿它判断渲染是错的。渲染只认用户端观感。

## 排查时的心态约束

- sidecar 日志两小时空白，**不能**推出「它没干活」——它只在状态切换时打一行。空白只说明没切换。
- 网关日志里 `Normal final-send NOT suppressed ... streamed=False previewed=False content_delivered=False`
  加上紧接着的 `Sending response (N chars)`，证明的是**宿主把完整内容发出去了**，
  同时说明**这一轮 sidecar 没有接手**（它自己的 `send_attempts` 不会为这一轮增长）。
  于是问题区间被压缩到「宿主之后、客户端之前」，**归因不能落在 sidecar 身上**，也不要往宿主上游找。
- 消失与长度的相关性值得先测一遍：若短回复全部到达、只有长回复（约 1.5k 字符以上、含表格）消失，
  嫌疑就落在「更新帧数触顶」或「多帧拼接失败」，而不是投递链路。这是**待验假设**，别当结论说。
- 要把结论讲到能被一句话否证的程度（"如果是 X，那 Y 计数应该 >0"），再去看那个计数。
