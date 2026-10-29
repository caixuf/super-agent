# 换一台机器，从零立起同等能力

这份清单只写**通用**步骤：谁 clone、装什么、哪些东西必须由部署方给值。本仓不含业务内容；
业务键（provider、平台开关、MCP、角色名册）由业务叠加层与机器本地文件提供。

## 0. 前置

- Linux + 已装好的 Hermes 运行时（`hermes --help` 能跑）；
- Python ≥ 3.11（仅 `--with-docker` 之外的检查需要）；
- 可选：docker（干净室验证）。

## 1. 一条命令复验一个干净 clone

```bash
git clone https://github.com/caixuf/super-agent.git && cd super-agent
python3 tools/redeploy_check.py --skip-docker     # 干净 clone / 全新 profile 安装 / 补丁 / verify.sh
python3 tools/redeploy_check.py                   # 再加干净室 docker/verify.sh
python3 tools/t6_gap_audit.py                     # T6 缺口逐条：PASS 还是 OPEN
```

退出码 = 失败项数；`[SKIP]` 只说明环境缺件（例如没有 docker），**不算通过**。

## 2. 装到目标 profile

```bash
bash install/install.sh --profile <name> --compose-soul <业务叠加层.md>
```

安装器做这些事（每步都可重入，不覆盖现场身份、不改本仓）：

| 步骤 | 结果 |
| :--- | :--- |
| ③ 工具集 | `toolsets/<name>/` → 部署位 |
| ⑤b 渲染引擎 | `tools/`（含 `mermaid.min.js`）→ 部署位 |
| ⑥ 运行时基线 | `runtime/` 骨架（占位符，不是现场配置） |
| ⑦⑧ 插件 | `plugins/<下划线名>/` → `<profile>/plugins/<连字符名>/`，并把带 `plugin.yaml` 的插件名写进 `<profile>/config.yaml` 的 `plugins.enabled` |
| ⑩ 宿主补丁 | 先 `apply_feishu_card_replies.py`，再 `patch_card_finalize.py`（顺序不可换） |

## 3. 插件从「装了」到「工具面里看得见」

这是最容易假绿的一步。三件事缺一不可：

1. **启用记录**：安装器已写 `plugins.enabled`；有官方 CLI 时优先 `hermes --profile <p> plugins enable <name>`。
2. **`plugins.entries.<name>.settings`**：按各插件 `README.md` 的模板补（`engine_root`、`queue_root`、
   `outbox_root` 等部署路径），安装器不猜这些值。
3. **工具面清单**：`platform_toolsets.<platform>` 若是一份**显式保存的清单**，新插件的工具集必须
   显式加进去，否则「插件已加载、工具不出现」：

   ```bash
   hermes --profile <p> config set platform_toolsets.api_server \
     '[..., "durable_dispatch", "markdown_delivery"]'
   ```

   然后**重启网关**（工具表在进程启动时装配）。

验收只看一件事：**工具面里出现那个工具**（一次真实调用或 `hermes tools list` 里能看到）。
配置文件里的那一行不算证据。

## 4. 只住在本机、不进仓的东西（必须逐项给值）

| 项 | 在哪 | 怎么来 |
| :--- | :--- | :--- |
| 模型/平台密钥 | `<profile>/.env`、`auth.json` | 部署方自填，永不入库 |
| MCP 注册项与凭据 | `<profile>/config.yaml` 的 `mcp_servers` | 按业务叠加层 |
| 插件 settings | `<profile>/config.yaml` 的 `plugins.entries` | 按各插件 README |
| 消息平台开关 | `platforms.*` | 部署方决定 |
| 定时作业 | `<profile>/cron/jobs.json` | 部署方按需 |
| systemd 单元与 drop-in | `~/.config/systemd/user/` | `hermes gateway install` 生成；环境开关另加 drop-in |
| 业务技能与角色名册 | `<profile>/skills`、`agentcore/agents` | 业务叠加层 |

## 4.1 定时 AI 作业：用 autotask 契约，不写自由提示词

`tools/autotask/` 把定时作业拆成五段：`collect`（只读输入）/ `judge`（唯一需要模型判断的一句）/
`write`（固定产物路径，只允许 `state/` 或 `skills/candidates/`）/ `report`（可判定的 `ok`/`fail`）/
`retry`（封顶）。作业是数据文件，`runtime/autotask/jobs/*.json` 有五个可直接用的模板。

```bash
python3 tools/autotask/taskfile.py validate runtime/autotask/jobs/*.json   # 契约自检
python3 tools/autotask/taskfile.py cron runtime/autotask/jobs/memory-budget-guard.json  # 转成宿主 cron 条目
```

把 `cron` 的输出合并进 `<profile>/cron/jobs.json` 即生效；调度仍归宿主，本模块不引入第二个调度器。

## 5. 故障速查

| 现象 | 原因 | 处理 |
| :--- | :--- | :--- |
| 消息平台里机器人「在线但不回」 | `FEISHU_ALLOWED_USERS` 不含你的身份，网关日志是 `Unauthorized user (ignored): Dropped a message from unrecognized` | 把身份加进该 profile `.env` 的 allowlist，重启网关 |
| 卡片表格显示「请升级客户端」 | 卡片 JSON 2.0 需要客户端 ≥ 7.20；开关没开也会回退 post | `HERMES_FEISHU_CARD_V2=1`（网关环境）+ 重启；关掉即回旧行为 |
| 卡片 PATCH 反复失败 | 更新前后都必须是共享卡：`config.update_multi: true` 每一帧都要带 | 用 `patches/patch_card_markdown_native.py`（已含） |
| 卡片表格被 API 拒 `200905` | `row_height: "auto"` 非法 | 用 `low/middle/high` 或像素值 |
| 卡片表格被拒 `200914` | 行数据与列类型不匹配（例如 `lark_md` 列给了对象） | 单元格一律字符串 |
| `md_deliver` 报 `99991672` | 应用缺上传素材作用域（`drive:drive` / `docs:document.media:upload` 等） | 在应用后台授权；错误信息会带平台返回的完整作用域列表 |
| `.md` 报告还是以附件发出 | 旧的 `##FILE:` 习惯；或转换失败后仍有客户端补发 | 规则：只给 `md_deliver` 返回的链接；relay 会把未转换的 `.md` 拦下（`markdown_not_converted`） |
| 插件工具报 `takes 0 positional arguments but 1 was given` | 工具按位置参数派发 `handler(args_dict, **kwargs)` | 处理器签名写成 `def call(args=None, **kwargs)` |
