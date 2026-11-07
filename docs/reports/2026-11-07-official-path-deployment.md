# 官方路径部署记录（bot1 / `default` profile，2026-11-07）

编制日期：2026-11-07（Asia/Shanghai）。
执行者：受控 Agent 会话，依据 `docs/superpowers/specs/2026-11-07-single-writer-deployment-design.md` §5。
目标实例：`/home/caixuf/.hermes`（profile `default`），宿主树
`/home/caixuf/super_agent/default/hermes/v0.22.5-bf0203a794a0`（官方 v0.22.5 + `bf0203a794a0`）。
仓内版本：`main` @ `c2d10fa`（部署期间新增 `plugins/lan_cluster` 修复，见 §5）。

**一句话结论**：部署按官方 `install/install.sh` 链路完成，退出码 0；能力默认值、插件装载、
补丁、引擎位置、单元与记忆配置全部由官方脚本写入，现场没有手写任何一行
`plugins.entries.*.settings`。工具面实测恢复（`md_deliver` / `md_resolve` / `lan_node_*` /
`colleague_*` 全部在位）。**有 3 个分叉需要运维方决策**：4 个现场增强版技能被官方安装器降级覆盖、
`task-progress` 因现场显式关闭而保持关闭、宿主树被官方补丁重写了 2 个文件。

---

## 1. 执行时间线

| 步 | 动作 | 结果 | 证据 |
| :-- | :--- | :--- | :--- |
| 0 | 备份（本次会话前完成） | `~/.hermes` 全量 + `~/super_agent/default` 全量 | `~/super_agent/migration-backups/migration-20261107-181933/`、`~/super_agent/backups/pre-official-20261107-175322/` |
| 0b | 部署前快照 | config + 插件/技能清单 | `~/super_agent/migration-backups/pre-official-install-20261107-193657/` |
| 1 | 仓库测试基线 | 3036 passed / 59 skipped（环境缺口） | 见 §9 |
| 2 | 官方 dry-run 逐行核对 | 通过，确认无意外写盘 | `install/dry-run.txt`（基线备份内） |
| 3 | **官方真装** | `EXIT=0` | `~/super_agent/migration-backups/official-install-20261107-193700.log` |
| 4 | 官方引擎路径迁移 | 10 处 | 同上日志之后单独执行 `install/migrate_engine_paths.py` |
| 5 | 官方单元/记忆生成 | worker + memory 单元重生成并激活 | `install/setup_worker.py` / `install/setup_memory.py` |
| 5b | 官方安装器重跑（传播 §5 修复） | `EXIT=0`，全量「已一致」 | `~/super_agent/migration-backups/official-install-rerun-20261107-194235.log` |
| 6 | 重启 + 工具面验收 | 通过 | §6 |
| 7 | 冻结受管 release 根 | 已冻结（`a-w`） | §4.14 |

### 实际执行的官方命令

```bash
export HERMES_AGENT_DIR=/home/caixuf/super_agent/default/hermes/v0.22.5-bf0203a794a0
export PATH="$HERMES_AGENT_DIR/.hermes/bin:$PATH"     # 见 §4.1
bash install/install.sh --hermes-home /home/caixuf/.hermes

cd install && PYTHONPATH=$PWD python3 migrate_engine_paths.py \
  --config /home/caixuf/.hermes/config.yaml \
  --old-engine-root /home/caixuf/super_agent/default/current \
  --new-engine-root /home/caixuf/.hermes/agentcore \
  --replace /home/caixuf/code/super-agent/runtime/hermes_completion_transport.py=\
/home/caixuf/.hermes/agentcore/runtime/hermes_completion_transport.py

cd install && PYTHONPATH=$PWD python3 setup_worker.py \
  --profile-home /home/caixuf/.hermes --engine-root /home/caixuf/.hermes/agentcore \
  --profile default --python /usr/bin/python3 --apply --activate

cd install && PYTHONPATH=$PWD python3 setup_memory.py \
  --profile-home /home/caixuf/.hermes --engine-root /home/caixuf/.hermes/agentcore \
  --profile default --user caixuf --agent hermes \
  --embed-url http://127.0.0.1:8910 --python /usr/bin/python3 --apply --activate
```

---

## 2. 目标拓扑（迁移后实测）

| 项 | 迁移前 | 迁移后（实测） |
| :--- | :--- | :--- |
| 引擎 | `~/super_agent/default/current` → release 快照 | **`~/.hermes/agentcore`**（83M，含完整 `agentcore/` 包源码） |
| 插件 | 13 个符号链接指向 `releases/5331261` | **13 个实体目录副本**（安装器语义） |
| 能力默认值 | 受管部署器只写启用列表 | **官方 `plugin_defaults.py` + `configure_defaults.py`** |
| 补丁 | 受管部署器自维护顺序 | **官方 `install.sh` ⑩ 步**统一应用/校验 |
| 宿主树 | 候选 v0.22.5 | 路径不变，**但被官方补丁重写了 2 个文件**（§4.3） |
| 网关单元 | `hermes-gateway-default.service` | 不变（官方同名），drop-in `super-agent-cards.conf` 保留 |
| worker / memory 单元 | `superagent-worker-default` / `superagent-memory-default` | **官方脚本重生成**，名字一致 |
| 受管 release 根 | 在役 | **冻结为只读回退底本**（4 个 release，sha256 清单留存） |
| 嵌入服务 | `bge-m3-sidecar.service` → 127.0.0.1:8910 | 不变 |

---

## 3. 配置改动全清单（`~/.hermes/config.yaml`）

权威配置是 `~/.hermes/config.yaml`。写入者全部是官方脚本，**现场零手写**。

### 3.1 官方 `install_plugin_defaults.py` 新建的 entry（3 个）

| entry | 关键设置 |
| :--- | :--- |
| `colleague-team` | `enabled: true`、`engine_root: ~/.hermes/agentcore`、`team_id`、`primary_id: default`、`registry_path` |
| `markdown-delivery` | `enabled: true`、`engine_root`、`outbox_root`、`env_file: ~/.hermes/.env` |
| `lan-cluster` | `enabled: true`、`engine_root`、`state_dir: ~/.hermes/agentcore-state/lan-cluster` |

### 3.2 官方 `configure_defaults.py` 补齐的工具面接线（9 个平台 × 5 个工具集）

`cli` / `telegram` / `discord` / `whatsapp` / `slack` / `signal` / `homeassistant` / `qqbot` / `yuanbao`
各补入 `task_status`、`durable_dispatch`、`markdown_delivery`、`colleague_catalog`、`colleague_team`。

**`platform_toolsets` 里本来就没有 `feishu`** —— 目标 IM 平台走默认工具面，因此 7 个插件工具集在该平台上全部在位
（见 §6.1）。这也是 D05/D06 之前「配置里有、工具面没有」的历史成因之一。

### 3.3 官方 `migrate_engine_paths.py` 迁移的 10 处引擎内路径

| 位置 | 旧值 | 新值 |
| :--- | :--- | :--- |
| `plugins.entries.<8 个插件>.settings.engine_root` | `~/super_agent/default/current` | `~/.hermes/agentcore` |
| `plugins.entries.durable-dispatch.settings.delivery_transport_file` | 仓库检出路径 | `~/.hermes/agentcore/runtime/hermes_completion_transport.py` |
| `memory.localvec.recall_cmd[1]` | `~/super_agent/default/current/tools/recall_memory.py` | `~/.hermes/agentcore/tools/recall_memory.py` |

迁移后 `config.yaml` 内 **不再有任何 `super_agent/default/current` 或仓库检出路径**。

---

## 4. 分叉与偏离（逐条）

### 4.1 【分叉】官方插件启用入口的 CLI 版本被显式对齐到在役候选

- **现象**：官方安装器 ⑧ 步的插件启用优先走官方入口
  `HERMES_HOME=~/.hermes hermes --profile default plugins enable <name>`。
  判定「可寻址」的条件是 `~/.hermes/hermes-agent` 存在 —— 现场确实存在，但它是**退役的 v0.22.2 树**。
  PATH 上的 `hermes` = `~/.local/bin/hermes` → `~/.hermes/hermes-agent` → **v0.22.2**。
- **偏离**：若不处理，这条官方路径会用 v0.22.2 的 CLI 去写现场配置，而运行中的网关跑的是 v0.22.5。
- **处理**：把在役候选树自带的 `.hermes/bin` 前置到 PATH，官方入口因此与网关同版本。
- **证据**：安装日志 ① 步打印
  `Hermes CLI：/home/caixuf/super_agent/default/hermes/v0.22.5-bf0203a794a0/.hermes/bin/hermes  Hermes Agent v0.22.5 (2026.9.24) · stable · local bf0203a794a0`。
- **遗留**：PATH 上的 `~/.local/bin/hermes` **未改**，仍指向退役的 v0.22.2 树。直接敲 `hermes` 仍会打到旧树。
  验收请一律使用候选树自带 CLI（§6.1 的命令）。

### 4.2 【分叉】`task-progress` 保持关闭 —— 官方「保留显式关闭」语义

- 现场 `plugins.entries.task-progress.settings.enabled: false`（历史显式关闭）。
- 官方 `install/plugin_defaults.py::eligible_names()` 明确规定：默认安装**永不反转**显式关闭。
  因此该插件不进入启用列表，`plugins.enabled` 不含它，工具面没有 `task_progress`。
- 这不是安装缺陷，是官方契约。官方就绪门禁也如实报 `[OPEN] task-progress: missing: enabled`。
- **需要运维方决策**：是否要为 bot1 打开该能力（打开方式必须是改 `plugins.entries.task-progress.settings.enabled`，
  那是官方 `plugin_defaults.py` 的写入范围，不是现场手写）。

### 4.3 【分叉】宿主树被官方补丁重写了 2 个文件

- 设计 §3 写的是「宿主树不变」。**代码版本确实没换**（仍是 v0.22.5 + `bf0203a794a0`），
  但官方 ⑩ 步的 `patch_card_finalize.py` 在这台宿主树上 `--check` 原本 **FAIL（缺 6 处探针）**，
  官方脚本按自己的规则 `--apply` 重放了补丁。
- 落地差异（相对上游 HEAD）：`gateway/stream_consumer.py` +6/-2、`gateway/stream_consumer_transport.py` +29/-6。
- 重跑安装器复验：`[PASS] 卡片终态/同卡补丁：在位（11 处探针齐全）`。
- **语义**：宿主树从「已手工打过补丁的候选树」变成「官方安装器打过补丁的候选树」——这正是单一写者想要的结果，
  但它确实改动了宿主树文件，必须留痕。宿主树当前 `git status` 共 28 项（含补丁改动的 2 个文件与 7 个未跟踪文件）。

### 4.4 【分叉】4 个现场增强版技能被官方安装器降级覆盖

官方 `install_skill_tree` 的语义是「先备份到 `<home>/skill-backups/<name>.<ts>.<pid>`，再整棵替换」。
本次替换 5 个技能：

| 技能 | 现场旧版 | 仓内新版 | 判定 |
| :--- | :--- | :--- | :--- |
| `illustrated-doc-rendering` | 90 行 | 96 行（含「交付与渐进式交付」段，仓内 `ae49377` 提交） | **升级**，无损失 |
| `im-card-markdown` | 239 行 v1.1.0 | 65 行 v1.0.0 | **降级**，丢失 5 节 |
| `research-before-build` | 87 行 | 64 行 | **降级**，丢失「一之一」一节 |
| `skill-distillation` | 98 行 | 72 行 | **降级**，丢失「一之二」一节 |
| `wake-storm-guard` | 109 行 | 104 行 | **降级**，正文裁剪，无标题级丢失 |

- 现场版是**仓内没有的站点增强版**（仓内这 4 个技能只有 `8a1a3f0 Initial public release` 一次提交），
  官方安装器按设计覆盖了它们。
- **备份完整**：`~/.hermes/skill-backups/<name>.20261107-193706.62754/`。
- **需要运维方决策**：把现场增强版合并回仓内（或把仓内版本升到 1.1.0），
  否则以后每次官方安装都会再覆盖一次；否则就只能从备份回填，等于回到「现场手写」的老路。

### 4.5 【分叉】worker / memory 单元的 `--python` 用 `/usr/bin/python3`，不是私有 venv

- 官方 `install.sh` ⑩b/⑩c 默认用 `install/install.sh` 建的私有环境
  `~/.hermes/agentcore/.superagent-venv/bin/python`。
- 本次用 `/usr/bin/python3`（现场既有值），原因是：
  现场 `memory.localvec.recall_cmd` 的首元素就是 `/usr/bin/python3`；
  官方 `setup_memory.py` 会**逐项比对** recall_cmd，不一致直接报
  `existing recall command differs; migration required`；
  要让它一致就得手工改 `memory.localvec.recall_cmd` —— 那正是本次要消灭的「现场手写」。
- 用 `/usr/bin/python3` 时 recall_cmd **逐项相等**，`setup_memory.py` 判定「配置已是目标形态」，
  因此**它没有重写 config.yaml**（`config.yaml` 的 mtime 仍是路径迁移那一步的 19:38:19）。
- 已实测新引擎在 `/usr/bin/python3` 下可导入：
  `agentcore.pool` / `agentcore.memory.runtime_service` / `agentcore.ccp_analysis_model` /
  `agentcore.ccp_config` / `runtime.memory_bootstrap` / `tools.recall_memory` 全部 OK。
- 其余单元内容与现场旧版**只差一行**：`PYTHONPATH` 从 `~/super_agent/default/current` 改为 `~/.hermes/agentcore`。
- 旧单元先移开到 `~/super_agent/migration-backups/migration-20261107-181933/systemd-user/superseded-20261107-193922/`，
  官方脚本因此走「新建」路径（官方脚本本来就拒绝覆盖内容不同的既有文件）。

### 4.6 【分叉】⑩b / ⑩c / ⑩d 在 `--hermes-home` 模式被官方脚本跳过

- 官方安装器在 `--hermes-home` 模式下**不**为既有 profile 迁移 worker / 记忆 / 网关。
- 因此设计 §5 步骤 5 落到「单独调用官方 `setup_worker.py` / `setup_memory.py`」，不是安装器内部步。
- `--user caixuf --agent hermes`（现场单身份）而非安装器默认的 `--user $PROFILE --agent $PROFILE` ——
  这是现场既有值，官方脚本按它校验并判定一致。
- 网关单元**未**由本次部署改动（沿用现有 `hermes-gateway-default.service` + `super-agent-cards.conf` drop-in）。

### 4.7 【分叉】`delivery_transport_file` 迁移用官方 `--replace`

- 该值原本指向**仓库检出**（`/home/caixuf/code/super-agent/runtime/...`），不在旧引擎根下，
  所以要用官方 `migrate_engine_paths.py --replace OLD=NEW` 精确替换。
- 两个文件逐字节相同（21717 字节），纯路径迁移，零行为变化。
- **工具显示瑕疵**（本次部署时仍在）：该工具的 `--plan` 输出把 `--replace` 的目标统一显示成新引擎根
  （`plugins.entries.durable-dispatch.settings.delivery_transport_file -> /home/caixuf/.hermes/agentcore`），
  落盘值是完整的替换值。**只看 plan 行会误判，请核对配置文件。**
- **已修**（§10.1）：plan 现在打印**真实**落盘值，并有测试钉住。

### 4.8 【分叉】`memory.localvec.recall_cmd` 指向的脚本版本变了（行为等价）

- 旧 release 里的 `recall_memory.py`（3449 字节）→ 新部署副本（3787 字节，仓内 `main`）。
- 差异：新增 `--embed-url` 的 help 文本；**未指定 `--embed-url` 时不再隐式回落 `MemoryConfig` 默认**
  （改为关键词排序，不访问 Ollama），与 `docs/REDEPLOY.md` 的口径一致。
- 现场 recall_cmd **显式传** `--embed-url http://127.0.0.1:8910`，因此实际行为不变。
- 已实测迁移前后输出逐字一致：`{"ok": true, "results": [], "empty_reason": "below_score_floor"}`。

### 4.9 【偏离】`lan-cluster` 的官方默认与引擎契约冲突 —— 已在仓内修复（最终形态见 §10.1）

- `c2d10fa` 引入的官方默认启用 `lan-cluster`，但不写 `nodes`；
  而引擎 `agentcore/lan_cluster.py::normalize_nodes()` 原先对空映射抛 `lan_nodes_required`
  （`tests/test_lan_cluster.py` 明确断言该契约）。
- 后果：`register()` 抛异常 → 插件装载失败 → **`lan_cluster` 工具集不出现**，
  即 `c2d10fa` 提交信息里「lan_node_list/probe/rpc 工具面回归」的说法**当时并不成立**。
- **修复**：把「空集群」的语义收进**引擎**（`normalize_nodes(None / {})` → 空目录；
  已配置的映射仍逐项校验；`probe` / `rpc` 在空集群上明确报 `lan_nodes_required`），
  插件退回薄适配层。引擎契约与既有测试按 §10.1 同步更新。
- 修复经**重跑官方安装器**铺到现场（`~/.hermes/plugins/lan-cluster/__init__.py` 与
  `~/.hermes/agentcore/agentcore/lan_cluster.py` 均与仓内逐字节相同），并已复验。

### 4.10 【偏离】官方脚本的 `rm` 落到了运行时的可恢复删除通道

- 安装器里的 `rm -f` / `rm -rf`（摘符号链接、替换技能）被运行时路由为可恢复删除（回收站），
  而不是直接 unlink。
- 语义与官方脚本写的不同，但**对本部署无影响**：所有旧对象都还在，可随时取回。

### 4.11 【遗留】`~/.hermes/profiles/default/config.yaml` 是过期快照，官方安装器不写它

- 权威配置是 `~/.hermes/config.yaml`（部署前 311 行 → 部署后 374 行）。
  `~/.hermes/profiles/default/config.yaml`（349 字节、mtime 11-01）只是旧布局遗留，官方安装器不碰它。
- 它仍列着 `task-progress` 而权威配置没有 —— **手工翻这个文件会得出与现场相反的结论**。
  建议归档或加注释，但不要删（不在本次授权范围内）。

### 4.12 【遗留】`--apply-baseline` 未使用

- 官方 ⑪ 步默认不动现场配置，本次未给 `--apply-baseline`，因此
  `agent.max_turns` / `delegation.max_iterations` / `approvals.*` 等基线保持现场原值。
- ⑪ 步的「地板只抬不降」语义未触发。

### 4.13 【遗留】官方就绪门禁不覆盖 `lan-cluster`

`tools/deployment_readiness.py` 的能力清单里没有 `lan-cluster`（源码内无 `lan` 字样）。
`lan_cluster` 的验收只能靠工具面清单与真实调用，不能靠就绪门禁。
**已修**（§10.1）：门禁现在有 `lan-cluster` 一行，并按「空集群是合法状态」只要求
总开关 + 引擎根 + 状态目录。

### 4.14 【刻意】冻结范围只限受管 release 根，宿主树不冻结

- **已冻结**：`~/super_agent/default/releases/{10b55bb,5331261,dc398f7,e0e86f8}`
  （`chmod -R a-w`，共 1489 个文件的 sha256 清单存于
  `~/super_agent/migration-backups/migration-20261107-181933/release-root-manifest/`）。
  写入探针确认 `Permission denied`，`current` 符号链接仍可正常解析。
- **不冻结**：宿主树 `~/super_agent/default/hermes/v0.22.5-bf0203a794a0` ——
  网关进程运行时会在 `gateway/__pycache__` 等处写字节码，冻结会让网关起不来。
- 冻结前已确认：**没有任何运行中进程、配置文件或 systemd 单元再引用 `~/super_agent/default/current`**。
- 受管 release 根**未删除**，只读即可回退。

---

## 5. 仓内代码改动（本次部署期间）

| 文件 | 改动 | 理由 |
| :--- | :--- | :--- |
| `agentcore/lan_cluster.py` | 「空集群」语义收进引擎 | §4.9 / §10.1 |
| `plugins/lan_cluster/__init__.py` | 退回薄适配层（仅余 2 行注释差异） | 同上 |
| `install/migrate_engine_paths.py` | plan 打印真实落盘值 | §4.7 / §10.1 |
| `tools/deployment_readiness.py` | 门禁补 `lan-cluster` 一行 | §4.13 / §10.1 |
| `tests/test_lan_cluster.py` | 改 1 例（判例）+ 增 1 例 | §10.2 |
| `tests/test_lan_cluster_plugin.py` | 新增 7 例 | §4.9 |
| `tests/test_install_script.py` | 新增 1 例（plan 显示真实目标值） | §4.7 |
| `tests/test_deployment_readiness.py` | 新增 1 例（门禁覆盖 lan-cluster） | §4.13 |
| `docs/RELEASE_CAPABILITIES.md` | §10.1 逐项状态 | 能力声明 |
| `docs/reports/2026-11-07-official-path-deployment.md` | 本文件 | 部署留痕 |

未改动：`install/install.sh`、`install/plugin_defaults.py`、`install/configure_defaults.py`、
`install/setup_worker.py`、`install/setup_memory.py`、`install/enable_plugins.py`。

---

## 6. 验收证据

### 6.1 工具面（验收口径：**工具面里出现该工具才算生效**）

```bash
export HERMES_HOME=/home/caixuf/.hermes
C=/home/caixuf/super_agent/default/hermes/v0.22.5-bf0203a794a0/.hermes/bin/hermes
"$C" --profile default tools list --platform feishu
```

目标 IM 平台的插件工具集（部署前 4 个 → 部署后 7 个）：

| 工具集 | 部署前 | 部署后 |
| :--- | :--: | :--: |
| `colleague_catalog` | enabled | enabled |
| `durable_dispatch` | enabled | enabled |
| `memory_bridge` | enabled | enabled |
| `task_status` | enabled | enabled |
| `colleague_team` | — | **enabled**（新增） |
| `lan_cluster` | — | **enabled**（新增） |
| `markdown_delivery` | — | **enabled**（新增） |
| `a2a` | disabled | disabled（Hermes 默认关，非本仓插件） |

**单个工具的注册清单**（通过 Hermes 自己的插件管理器 `manager._plugin_tool_names` 读取，24 个）：

> `hermes tools list` 只列**工具集**，不列单个工具。要拿单个工具名，得在 Hermes 自己的
> 运行时里调插件管理器（`hermes_cli.plugins.get_plugin_manager()._plugin_tool_names`）——
> 这套 key 正是 `tools list` 判定「某插件工具集在本平台启用」的依据。

```
a2a_call  a2a_discover  a2a_history  a2a_list  a2a_orchestrate
colleague_catalog  colleague_define_role  colleague_grant  colleague_hire
colleague_list  colleague_retire
durable_cancel  durable_roster  durable_status  durable_submit
lan_node_list  lan_node_probe  lan_node_rpc          ← D06 恢复
md_deliver  md_resolve                             ← D05 恢复
memory_get_observations_l3  memory_search_l1  memory_timeline_l2
task_status
```

### 6.2 网关

`~/.hermes/gateway_state.json`（19:43 采样）：

```
gateway_state = running        pid = 92468
platforms/feishu/state = connected     needs_attention = False     error_code = None
session_store/status = ok
code_sha = bf0203a794a07aac3ff764972ab9087baa4e4f4e     code_version = 0.22.5
```

### 6.3 服务

| 单元 | 状态 | PYTHONPATH（实测 `/proc/<pid>/environ`） |
| :--- | :--- | :--- |
| `hermes-gateway-default.service` | active（19:42:43 重启） | 由宿主树提供，`HERMES_HOME=/home/caixuf/.hermes` |
| `superagent-worker-default.service` | active（19:39:36 重启） | `/home/caixuf/.hermes/agentcore` |
| `superagent-memory-default.service` | active（19:39:36 重启） | `/home/caixuf/.hermes/agentcore` |
| `bge-m3-sidecar.service` | active | — |
| `litellm-ccp.service` | active | — |

- 队列存量：`jobs` 总数 4，**非终态 0**（官方 `setup_worker.py --activate` 的前置条件已满足）。
- worker 停机日志 `{"attempts_finished": 0, "backend": "stdlib"}` —— 新引擎下正常收尾。

### 6.4 记忆召回（迁移前后逐字一致）

```bash
/usr/bin/python3 /home/caixuf/.hermes/agentcore/tools/recall_memory.py \
  --query-text='部署' --data-dir /home/caixuf/.hermes/state/superagent-memory/data \
  --user caixuf --agent hermes --profile default --embed-url http://127.0.0.1:8910 --limit 3
# => {"ok": true, "results": [], "empty_reason": "below_score_floor"}
```

### 6.5 官方就绪门禁

```bash
~/.hermes/agentcore/.superagent-venv/bin/python ~/.hermes/agentcore/tools/deployment_readiness.py \
  --profile-home /home/caixuf/.hermes --profile default
```

退出码 0。逐项：9 项 `[CONFIGURED]`（均带「runtime probe still required」尾巴，
含 §10.1 补上的 `lan-cluster`）、
durable-worker / memory-recall / memory-ingestion-worker / memory-store-alignment /
completion-delivery 均为 `[CONFIGURED]`；**2 项 `[OPEN]`**：
`task-progress: missing: enabled`（§4.2）与
`colleague-team: real provisioning and lifecycle integration is not established`（需真实编制/生命周期验收）。

门禁末尾自带声明：*Configuration checks are not proof of real model, platform or end-to-end delivery.*

### 6.6 补丁步

重跑安装器后的官方复验输出：

```
[PASS] 消息平台卡片补丁：在位（探针 {'_build_reply_card': True, 'card_replies': True, 'exec_approval_text': True}）
[PASS] 卡片终态/同卡补丁：在位（11 处探针齐全）
[PASS] 卡片原地更新重试补丁：在位
Feishu delivery UUID patch already installed
card lifecycle present
card delivery guard present
```

`patches/patch_card_markdown_native.py` 在 v0.22.5 形态上仍是「半途失败」，
但**官方 ⑩ 步不调用它**（该变量只赋值不执行，改用 `card_replies` 版本校验）——不是本次部署的阻断项。

### 6.7 完成回执积压只读核查（官方 ⑫ 步要求）

网关日志每分钟一条 `completion delivery backlog: parked=4 outcomes=- status=empty`。
按官方 ⑫ 步第 6 条跑只读诊断：

```bash
PYTHONPATH=/home/caixuf/.hermes/agentcore python3 -m agentcore.completion_backfill \
  --root /home/caixuf/.hermes/var/durable-queue --profile default
```

4 条全部 `status=completed` 且 **`deliverable=false`**，会话来自 2026-10-29（其中一条 `session=null`）。
即：**永远不会自动投递的存量无主回执**（历史遗留），不是本次部署引入的回归 ——
同一告警在部署前的 19:37 日志里已经存在。按 `docs/COMPLETION_DELIVERY.md` §2 走带证明的管理性修复，
**不在本次部署范围内**。

### 6.8 仓库测试

本次部署期间改了 `plugins/lan_cluster/__init__.py` 并新增 `tests/test_lan_cluster_plugin.py`，
因此**部署后重跑了全量** `python3 -m pytest tests/ -q`（结果见 §9）。

---

## 7. 回滚

### 7.1 一键回退物料

| 物料 | 路径 |
| :--- | :--- |
| `~/.hermes` 全量备份 | `~/super_agent/migration-backups/migration-20261107-181933/hermes-home/` |
| `~/super_agent/default` 全量备份 | `~/super_agent/migration-backups/migration-20261107-181933/managed-default/` |
| 部署前配置快照 | `~/super_agent/migration-backups/pre-official-install-20261107-193657/config.yaml.before` |
| 旧 systemd 单元 + runner.json | `~/super_agent/migration-backups/migration-20261107-181933/systemd-user/superseded-20261107-193922/` |
| 现场旧版技能（5 个） | `~/.hermes/skill-backups/<name>.20261107-193706.62754/` |
| 受管 release 根（已冻结只读） | `~/super_agent/default/releases/{10b55bb,5331261,dc398f7,e0e86f8}` |
| release 根 sha256 清单 | `~/super_agent/migration-backups/migration-20261107-181933/release-root-manifest/` |
| 首次安装器日志 | `~/super_agent/migration-backups/official-install-20261107-193700.log` |
| 重跑安装器日志 | `~/super_agent/migration-backups/official-install-rerun-20261107-194235.log` |

### 7.2 回滚步骤

1. 停网关：`systemctl --user stop hermes-gateway-default.service`
2. 恢复配置：`cp ~/super_agent/migration-backups/pre-official-install-20261107-193657/config.yaml.before ~/.hermes/config.yaml`
3. 恢复单元：把 `superseded-20261107-193922/` 里的 2 个 `.service` 与 `runner.json` 拷回原位；
   `systemctl --user daemon-reload`
4. 恢复引擎指向：`~/.hermes/agentcore` 可整体移走（它就是新装的副本）；
   恢复 release 根写权限（`chmod -R u+w ~/super_agent/default/releases`）
5. 恢复插件链接（如需完全回到旧形态）：`~/.hermes/plugins/*` 已被换成实体副本，
   旧的符号链接目标仍在 `~/super_agent/default/releases/5331261/plugins/`
6. 恢复技能：从 `~/.hermes/skill-backups/` 拷回（若不接受 §4.4 的降级）
7. 起服务：`systemctl --user start hermes-gateway-default.service superagent-worker-default.service superagent-memory-default.service`
8. 复验：目标 IM 平台连通 + 工具面回到部署前的 4 个插件工具集

### 7.3 单点回滚（不动全局）

- 只回滚能力默认值：把 `~/.hermes/config.yaml` 的 `plugins.entries` 段恢复为
  `pre-official-install-20261107-193657/config.yaml.before` 的对应段。
- 只回滚某个插件的开关：在 `plugins.entries.<name>.settings` 下改 `enabled` —— 但这属于**现场手写**，
  与本次单一写者原则冲突，只作为应急手段。

---

## 8. 待运维方决策 / 待人工验收

### 8.1 需要决策

1. **4 个现场增强版技能是否合并回仓内**（§4.4）。不合并 = 每次官方安装都覆盖一次。
2. **`task-progress` 是否为 bot1 打开**（§4.2）。官方默认不会反转显式关闭。
3. **`local-model-grounding` 的判别模型后端**（缺后端就不开，现状 = 关）。
   它仍在 `plugins.enabled` 列表里（官方 `plugins list` 显示 "enabled"），
   但**没有** `plugins.entries.local-model-grounding` → 总开关缺省 false → 工具面不出现。
   这与 `docs/RELEASE_CAPABILITIES.md` §10.1 的「关闭」口径一致，但**列表与实际不一致**，
   排查时容易误读，已在该文件 §10.1 注明。
4. **PATH 上的 `hermes` 是否指向候选 v0.22.5**（§4.1）。现状仍指向退役的 v0.22.2。

### 8.2 需要人工验收（自动化不能替代）

1. 真实消息通道：发 → 回读 → 幂等 → 撤回 → 投递（`docs/REDEPLOY.md` §3）。
2. IM 卡片视觉与按钮回调（`HERMES_FEISHU_CARD_V2=1` 生效）。
3. `colleague-team` 的真实编制与生命周期（就绪门禁 `[OPEN]` 项）。
4. 记忆召回的语义排序（当前门禁只验了命令存在与 embed 端点）。

### 8.3 本次未做（明确不做）

- 不删除受管 release 根（只冻结）。
- 不冻结宿主树（网关运行时会写 `__pycache__`）。
- 不手工写 `plugins.entries.*.settings`（全部交给官方写者）。
- 不下载判别模型来开 `local-model-grounding`。

---

## 9. 仓库测试

部署期间改了 `plugins/lan_cluster/__init__.py`、新增 `tests/test_lan_cluster_plugin.py`
与两份文档，因此**部署后重跑了全量**：

```bash
python3 -m pytest tests/ -q
# 3046 passed, 59 skipped in 575.99s (0:09:35)   退出码 0
```

对比部署前基线 `3036 passed / 59 skipped`：**+10** 来自本次新增/改写的用例
（`tests/test_lan_cluster_plugin.py` 7、`tests/test_lan_cluster.py` 1、
`tests/test_install_script.py` 1、`tests/test_deployment_readiness.py` 1）。

**中途踩过一次门禁**（记录在案）：部署记录初稿里写了业务字面量（消息平台的产品名），
被仓库自带的 `tests/test_repo_neutrality.py` 拦下（`1 failed, 3040 passed`）。
已把文档里的业务字面量改成中性表述后重跑通过 —— 这条门禁是有牙齿的，
文档里出现站点指纹同样算失败。

**59 个 skip 的口径**（都是环境缺口，**不算通过**）：

| 数量 | 原因 |
| --: | :--- |
| 8 | 可选 native binding 未安装（`test_scheduler.py`） |
| 5 | 缺 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_TEST_CHAT_ID` |
| 4 | 无 `flowcoro_py` |
| 3 + 9 | 需 `HERMES_SOURCE` 指向真实 Hermes 宿主 |
| 3 | Hermes 源码树不可用（`test_setup_memory.py` / `test_install_script.py`） |
| 2 | 真实同事桥接检出不可用 |
| 2 | 退役宿主树 `~/.hermes/hermes-agent` 缺 `plugin_messages.py`（宿主版本不带该契约） |
| 1 | 缺 `python-markdown` |
| 1 | 缺 `ruamel` |
| 1 | verified card baseline 不可用 |
| 1 | 浏览器抓取依赖不可用 |

其中「缺 `ruamel`」对应候选树自带 Python 的已知缺口：
`patches/verify_card_finalize.py` 依赖 `ruamel.yaml`，**在候选树里直接跑不了**，
契约验证只能靠安装器 ⑩ 步的 `--check` 输出（本次 11 处探针齐全）。

---

## 10. 部署后追加的官方代码修复

部署收尾时发现的三处问题，都在我们自己仓内的官方代码里，且都在本次部署里留下了真实痕迹。
经确认后**直接在官方代码里修**（仓内 super-agent 就是官方源），修完同样走官方安装器铺到现场。

### 10.1 三处修复

| # | 文件 | 问题 | 修法 |
| :-- | :--- | :--- | :--- |
| ① | `agentcore/lan_cluster.py` + `plugins/lan_cluster/__init__.py` | 「空集群」语义**分在两层**：插件层说 `{}` 是空集群，引擎层说 `{}` 是错误 | 语义**收进引擎**：`normalize_nodes(None / {})` → 空目录；已配置的映射仍逐项校验；`probe` / `rpc` 在空集群上明确报 `lan_nodes_required`。插件退回薄适配层（只余 2 行注释差异） |
| ② | `install/migrate_engine_paths.py` | `--replace` 的 `--plan` 一律印新引擎根，看 plan 会误判真实落盘值 | plan 打印**真实**目标值（`changed` 改为携带目标值的二元组） |
| ③ | `tools/deployment_readiness.py` | 官方就绪门禁对 `lan-cluster` 是哑的，坏了也不报 | 增加 `lan-cluster` 一行；只要求总开关 + 引擎根 + 状态目录（节点名册是运维方给的，空集群合法） |

### 10.2 ① 改了一条被测试钉住的契约 —— 这是改判例，不是加功能

- **原契约**：`tests/test_lan_cluster.py::test_normalize_nodes_rejects_invalid_specs`
  断言 `normalize_nodes({})` 抛 `lan_nodes_required`。
- **现契约**：`normalize_nodes(None)` 与 `normalize_nodes({})` 都返回空目录；
  非映射的输入抛 `lan_nodes_must_be_mapping`；
  **已配置**的映射仍逐项校验，畸形项照旧拒绝（`invalid_node_id` 等不变）。
- **语义归属**：引擎判「这是不是一份合法的节点名册」，插件不再自己解释一遍。
  「配置了集群就必须有节点」这条判例**没有被削弱** —— 它现在由
  `probe_node` / `invoke_node_rpc` 在空集群上抛 `lan_nodes_required` 来守。
- **判例由「映射为空即错」改为「映射为空即空集群，映射畸形才错」** ——
  这是本次唯一一处**修改既有判例**，故单列在此。
- 测试：`tests/test_lan_cluster.py` 改 1 例 + 增 1 例；
  新增 `tests/test_lan_cluster_plugin.py`（7 例）。

### 10.3 落地方式

改动全部在**仓内官方代码**，没有任何手写现场配置：

```bash
export HERMES_AGENT_DIR=/home/caixuf/super_agent/default/hermes/v0.22.5-bf0203a794a0
export PATH="$HERMES_AGENT_DIR/.hermes/bin:$PATH"
bash install/install.sh --hermes-home /home/caixuf/.hermes     # EXIT=0
systemctl --user restart hermes-gateway-default.service
```

复验（现场实测）：

- `~/.hermes/plugins/lan-cluster/__init__.py` 与
  `~/.hermes/agentcore/agentcore/lan_cluster.py` 均与仓内**逐字节相同**；
- 插件工具集 7 个在位，`lan_node_list` / `lan_node_probe` / `lan_node_rpc` 注册在位（共 24 个插件工具）；
- 官方就绪门禁新增一行：`[CONFIGURED] lan-cluster: configured; runtime probe still required`，退出码 0；
- 网关 `running`、IM 平台 `connected`、`needs_attention=false`。

### 10.4 与本次部署的关系

- ①②③ 都**没有**触碰现场配置的写入方式 —— 能力默认值、插件装载、补丁、单元与记忆配置
  仍然只由官方安装器写。
- ① 修的是**官方默认与官方引擎之间的契约错配**：`install/plugin_defaults.py` 的默认
  （启用 lan-cluster、不写 nodes）与引擎原先的「空映射即错」互相打架，
  结果是配置说开了、工具面说没有。修完之后，`plugin_defaults.py` 里那句
  *"An empty cluster is a valid cluster"* 才**真的成立**。
- 替代方案（也完全官方）：把 `lan-cluster` 从 `plugin_defaults.py` 的 `defaults()` 里去掉，
  默认不启用。代价是 `lan_node_*` 工具面永远不出现，
  与设计 §4.1「恢复 D06 工具面」相反。**本次按设计选前者。**
