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

## 5. 故障速查

| 现象 | 原因 | 处理 |
| :--- | :--- | :--- |
| 插件已启用、工具不出现 | `platform_toolsets.<platform>` 显式清单里没有它；或没重启网关 | 第 3 节：加清单 + 重启 |
| 插件工具报 `takes 0 positional arguments but 1 was given` | 工具按位置参数派发 `handler(args_dict, **kwargs)` | 处理器签名写成 `def call(args=None, **kwargs)` |
| 卡片表格显示「请升级客户端」 | 卡片 JSON 1.0 不渲染表格；需 2.0 | 设 `HERMES_FEISHU_CARD_V2=1`（网关环境）+ 重启 |
| 卡片表格被 API 拒 `200905` | `row_height: "auto"` 非法 | 用 `low/middle/high` 或像素值 |
| `.md` 报告以附件发出、打开是源码 | 没走文档导入 | 用 `md_deliver`，见 `plugins/markdown_delivery/README.md` |
