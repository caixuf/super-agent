# 换一台机器，从零立起同等能力

这份清单只写**通用**步骤：谁 clone、装什么、哪些东西必须由部署方给值。本仓不含业务内容；
业务键（provider、平台开关、MCP、角色名册）由业务叠加层与机器本地文件提供。

## 0. 前置

- Linux + 已装好的 Hermes 运行时（`hermes --help` 能跑）；
- Python ≥ 3.11，具备 venv/pip 和安装 Python 依赖的网络；安装器默认在部署目录建立私有运行环境。
  若使用 `--skip-deps`，则需要系统已提供 PyYAML、Markdown 等依赖；
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
bash install/install.sh --profile <name> --compose-soul <业务叠加层.md> \
  --analysis-root /absolute/approved/project
```

`--analysis-root` 提供持久分析可读取的目录；缺少它时不会猜测文件访问范围。
已有读取范围和显式关闭保持不变，若与指定目录冲突会报错。
带 `--analysis-root` 的完整补丁安装会为未配置过回传的 profile 设置直接回传，
并只处理设置后的新任务；既有回传策略不会被覆盖。必须重启网关并完成真实通道验收。

完整安装遇到不存在的具名 profile 时，会通过 Hermes 创建它，设置该新 profile 使用独立网关，
并安装、启动和检查网关。Hermes 将独立网关标记为暂时兼容模式；如果已有多 profile 网关
尚未完成重新扫描，安装会报错并保留待完成标记，重新扫描后可重跑。已有网关不会被重启。
新 profile 的模型和渠道凭据仍需由部署方提供；
`--skip-gateway` 可跳过新网关启动。

安装器做这些事（每步都可重入，不覆盖现场身份、不改本仓）：

| 步骤 | 结果 |
| :--- | :--- |
| ③ 工具集 | `toolsets/<name>/` → 部署位 |
| ②b 渲染依赖 | 私有环境安装 Python 依赖，验证可启动的浏览器；缺少时下载 Chromium（需要额外空间和网络） |
| ③b 引擎源码 | `agentcore/` → 部署位，可作为插件的 `engine_root` |
| ⑤b 渲染引擎 | `tools/`（含 `mermaid.min.js`）→ 部署位 |
| ⑥ 运行时基线 | `runtime/` 骨架（占位符，不是现场配置） |
| ⑦⑧ 插件 | 安装副本与启用记录；保留显式关闭项，并为缺失 entry 或设置项补安全默认值 |
| ⑩ 宿主补丁 | 先 `apply_feishu_card_replies.py`，再 `patch_card_finalize.py`、原生表格、重试及交付 UUID 补丁（顺序不可换） |

安装结束后运行安装器输出的就绪检查命令（使用部署目录中的私有 Python 环境）
检查已知缺口。它只检查配置和 worker 服务状态，不能替代真实工具、模型和消息通道验收；
存在缺口时返回非零。

## 3. 插件从「装了」到「工具面里看得见」

这是最容易假绿的一步。三件事缺一不可：

1. **启用记录**：安装器已写 `plugins.enabled`；有官方 CLI 时优先 `hermes --profile <p> plugins enable <name>`。
2. **`plugins.entries.<name>.settings`**：安装器为任务状态、进度、长任务守卫、图文提示和同事插件补充安全默认值，保留显式关闭和已有值。队列、授权范围、凭据及外部服务等仍需按插件 `README.md` 配置。
3. **工具面清单**：`platform_toolsets.<platform>` 若是一份**显式保存的清单**，新插件的工具集必须
   显式加进去，否则「插件已加载、工具不出现」。安装器会将已启用的任务状态、持久派工、文档交付和同事工具集补入已有平台清单；其他工具集仍须按需加入：

   ```bash
   hermes --profile <p> config set platform_toolsets.api_server \
     '[..., "durable_dispatch", "markdown_delivery"]'
   ```

   然后**重启网关**（工具表在进程启动时装配）。

验收只看一件事：**工具面里出现那个工具**（一次真实调用或 `hermes tools list` 里能看到）。
配置文件里的那一行不算证据。

`bash install/install.sh --require-ready` 会生成只读静态配置报告，并因运行时注册与端到端投递
尚未有独立证据而失败关闭。该选项适合把“未证实”挡在验收门外，不代表安装器能启动或验证生产服务。

Feishu 卡片的外部 HFC 运行态另走 [`integrations/hfc/README.md`](../integrations/hfc/README.md)；
`tools/hfc_acceptance.py` 检查锁定版本、Hermes 兼容、sidecar 健康和观测到的回合样本。
它不发送测试消息，客户端视觉表现、按钮回调和重复投递仍需私有测试会话人工验收。

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


### 持久任务 worker 的配置生成

对已配置 `durable-dispatch` 的具名 profile，安装器会根据现有队列根和授权读取范围生成
CCP worker 配置及 user systemd 单元并启动；`--skip-worker` 可跳过。队列存在未终止任务、
已有配置不同或 systemd 不可用时会报错，需核对后重试。启动前会以用户服务的环境检查 CCP 凭据，
通过本地模型目录接口验证 flash/main 两条路由；这不等于模型生成质量或回传已经验收。
工具不复制凭据，也不会覆盖已有的不同文件。
也可以单独预览：

```bash
python3 install/setup_worker.py --profile-home ~/.hermes/profiles/<name> \
  --engine-root ~/.hermes/profiles/<name>/agentcore --profile <name>
# 检查计划、模型端点与队列存量后，加 --apply 生成文件；再启用服务。
```

生成文件不表示服务正在运行或模型、消息回执已验收；只有真实提交、执行和结果投递都经过
验证才能算链路可用。模型适配使用 CCP flash/main，服务必须能读取其现有凭据。

### 自动记忆接线状态

`runtime/memory_bootstrap.py` 提供受限单身份 worker 的宿主配置；
`tools/recall_memory.py` 从同一个身份作用域的事实库提供 localvec 召回。
隔离测试覆盖了完成回合入队、模拟模型提取、入库以及同身份召回。完整安装的新 profile
会为其创建专用存储和 worker、配置同一身份作用域的 localvec 召回，并检查模型和本地 BGE
嵌入服务；默认地址是 `http://127.0.0.1:8910`，可通过 `--memory-embed-url` 指定本地服务，
`--skip-memory` 可跳过。已有 provider、存储和历史积压不会自动迁移；需完成有备份的迁移与
真实模型评估，不能把文件安装或服务启动当成生产自动记忆已验收。

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
