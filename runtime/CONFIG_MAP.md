# 配置地图 —— 哪些段归谁、哪些必须现场重测

现场 `config.yaml` 有几十个段。本文件回答三件事：**这一段是通用基座还是随宿主漂移 → 迁移动作 → 漏了会怎样**。

键名一律用 Hermes 的通用段名；**业务键（provider 名 / MCP 工具面 / 渠道凭据）由业务叠加层给**，本仓不列。

## 0. 三条前提

1. **`config.yaml` 在 git 之外**（内联着 provider / MCP 段与密钥）→ clone 带不走它，漏了机器人**能起来但变弱**。
2. **只走官方写入口**（`hermes config set / unset`）。手编一个缩进就能把现场网关弄坏。
3. **现场值一律重测**（窗口 / 压缩阈值 / 并发预算随机器与算力漂移），不照搬旧机数字。

## 1. 段 → 归属 → 漏了会怎样

| 段 / 键 | 归属 | 迁移动作 | 漏了会怎样 |
| :--- | :--- | :--- | :--- |
| `agent.*`（轮次 / 迭代 / 超时） | 通用基座 | `install.sh --apply-baseline` 抬到地板 | 长任务（证据→根因→补丁→验证）被中途截断 |
| `delegation.*`（backend / options / 并发 / 深度） | 通用基座，但**协议随 provider** | 见 §3 | 派工派不出去，或报「模型不存在」 |
| `approvals.*`（mode / timeout） | 通用基座 | 无人值守档 = `off` | 每步都等人点确认 → 无人值守停摆 |
| `security.protected_instruction_files` | **安全开关** | 人工决定 + 留痕 | 静默失去指令写入门；或反向：提示注入拦网被移除 |
| `max_concurrent_sessions` / `group_sessions_per_user` | 通用基座（容量） | 见 §2 | 会话到顶拿干净错误 / 群聊互相打断 |
| `model.*`、`fallback.*` | 随宿主 | 现场重配 | 没有可用模型 → 根本起不来 |
| 上下文窗口 / `compression.*` / `prompt_caching.*` | 随宿主 | **重测** | 长会话被过早压缩（丢证据）或成本失控 |
| `tool_loop_guardrails.*` | 通用基座 | 保底值 | 工具自旋烧 token |
| `memory.*`（含嵌入后端与维度） | 引擎通用，**后端随宿主** | 与库同源 | **召回全空但服务看起来正常**（见 MIGRATION ④） |
| `skills.*` | 通用 + 业务源 | 安装器 + 挂钩 | 技能缺失（业务技能走挂钩） |
| `platform_toolsets.*` / 各平台段 | 随平台 | 现场重配 | 渠道能力缺失 |
| `streaming.*` / `display.*` | 通用基座 | **有的改动要重启网关才生效** | 消息形态静默退化（最难查） |
| `cron`（作业定义在 `profiles/<p>/cron/jobs.json`） | 业务叠加层的部署清单 | 清单化 | 巡检 / 告警静音，故障无人知 |
| MCP / 插件 | **业务工具面** | 见 `docs/PLUGINS.md` | 工具面缺失（机器人"变哑"） |

## 2. 并行会话的真实语义（别按直觉猜）

读完 Hermes 自带的配置说明，确认如下：

- **准入**：同一会话同一时刻只有一轮（会话内串行）；**不同会话可以并行**。
- `max_concurrent_sessions`：**0 / null / 省略 = 不限**。取到上限时，**新会话拿到一个干净错误**，
  已有会话不受影响 —— 所以「到顶」的表现是**报错**，不是排队（别按"排队"去排障）。
  顶层键优先于 `gateway.max_concurrent_sessions`；它是 **best-effort 的单机/profile 运行时守卫**，
  读不到本地租约注册表时**fail open**（失败放行，不是失败拒绝）。
- `group_sessions_per_user`：为 `true` 时群聊里**每个参与者各占一个会话**（安全默认，防同群串话）；
  为 `false` 则整群共用一个会话 —— 人多时体验与成本差异极大。

**纪律**：会话槽位不是算力。长任务（跑批 / 编译 / 回灌 / 大范围检索）**必须后台化**
（建单 / 派工 / 后台进程 + 看门狗），不许占着会话槽位 —— 上限设得再高也救不回「发一句、等三分钟」。

> 坑（实测）：会话状态落 `state.db`（WAL）。**不要把活跃 profile 目录热删**：被删掉却仍被进程持有的
> WAL inode 会让该 profile 下次启动直接 `profile_parked`。恢复路径是停掉持有者（`hermes doctor`
> 会点名 PID）或**换一个 profile 名**，**不要手工删 WAL**。

## 3. 派工（delegation）的协议陷阱

- `delegation.backend: model_map` 时，`options` 必须是 **mapping**（写成 list 会被拒）。
- **provider 的 `api_mode` 决定端点**：codex 系走 `/v1/responses`，chat 系走 `/v1/chat/completions`。
  用错协议会报 401「模型不存在」/「No connected db.」—— 那是**协议错配，不是 key 坏**。
- 子 agent **不知道**父会话的任何上下文：派工时必须把角色契约全文内联进 context；
  工具面是否继承看开关（MCP 工具集继承）。
- 子 agent 只能做叶子：给「可被回执验证」的单件事，别让它再拆分。

## 4. 谁负责把这份地图变成现场值

| 角色 | 动作 |
| :--- | :--- |
| 部署方 | 按 §1 逐段重配；通用基座走 `--apply-baseline` |
| 业务叠加层 | 提供业务键（provider / MCP / 渠道凭据）与 cron 清单 |
| 巡检作业 | 检测能力基线被改小 → 原子回写（见 `BASELINE.md` §3） |
| 人 | 只决定**不可自动化的**那一类：安全开关、凭据、以及"这台机器要跑什么" |