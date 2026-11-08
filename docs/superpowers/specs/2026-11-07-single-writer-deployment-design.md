# 单一写者部署路径（bot1 切官方 install.sh）

> [!NOTE] **架构演进说明（2026-11-08）**
> 本规格记录 2026-11-07 针对多写者冲突实施的单一写者过渡设计（方案 B）。
> 系统随后在 2026-11-08 全面收敛演进为 **v2.0 单根受管架构（Single-Root Architecture，`~/super_agent/<profile>`）**，
> 统一由 `install/superagent.sh` 实施不可变版本 Release 切换、`hermes-current` 解释器动态推断、16 款插件与 `localvec` 记忆提供方挂载、`identity/SOUL.generic.md` 暂存与业务叠加层挂载，以及 `scripts/ops/_layout.sh` 布局感知运维。
> 最新权威规范请参阅 [`docs/ARCHITECTURE.md`](../../ARCHITECTURE.md)、[`docs/DEPLOY.md`](../../DEPLOY.md)、[`docs/PLUGINS.md`](../../PLUGINS.md) 与 [`docs/OPERATIONS.md`](../../OPERATIONS.md)。

编制日期：2026-11-07（Asia/Shanghai）。
状态：历史设计归档（过渡方案 B；当前已被 v2.0 单根架构收敛）。

## 1. 问题

`~/.hermes/config.yaml` 这一个文件，历史上被六个不同的写者分别写过：

| # | 写者 | 写哪一部分 | 受管路径是否调用 |
| :-- | :--- | :--- | :--: |
| 1 | `tools/superagent.py` → `install/enable_plugins.py` | `plugins.enabled` 列表 | 是 |
| 2 | `tools/superagent.py` → `migrate_config` | 路径标量 / `engine_root` | 是 |
| 3 | `install/plugin_defaults.py`（官方） | `plugins.entries.*.settings` 总开关 | **否** |
| 4 | `install/configure_defaults.py`（官方） | durable-dispatch 默认 + 工具面接线 | **否** |
| 5 | 现场手工编辑 | `denied_globs`、`roles`、`embed_url` 等 | — |
| 6 | `install/setup_worker.py` / `setup_memory.py`（官方） | 单元与记忆配置 | 是 |

六个写者各写各的字段，谁都不看别人写了什么，于是：

- 9 个以 `settings.enabled` 为总开关的插件，只有「启用列表」没有「总开关」，
  `ctx.get_config("enabled", False)` 返回 false，`register()` 提前返回 ——
  **工具面为空**（2026-11-07 实测：`markdown-delivery`、`lan-cluster`、
  `colleague-team`、`local-model-grounding`）。
- 每次部署结果都不一样，运维方必须逐项人工核对，这就是「总是有各种不一致」的来源。

## 2. 目标

**一个写者。** 能力默认值、工具面接线、插件装载、补丁应用、引擎位置、单元与记忆配置，
全部由官方 `install/install.sh` 链路负责；现场不再手工写 `plugins.entries.*.settings`。

验收口径沿用官方：**工具面里出现该工具才算生效**，配置文件里那一行不是证据
（`hermes tools list` / `hermes plugins list`）。

## 3. 目标拓扑

| 项 | 迁移前 | 迁移后 |
| :--- | :--- | :--- |
| 引擎（agentcore） | `~/super_agent/default/current` → release 快照 | **`~/.hermes/agentcore`**（官方默认位置，实体副本） |
| 插件 | 软链到 release | **实体副本**（`install.sh` ⑧ 步官方语义） |
| 能力默认值 | 受管部署器只写启用列表 | **官方 `plugin_defaults.py` + `configure_defaults.py`** |
| 补丁 | 受管部署器自己维护应用顺序 | **官方 `install.sh` ⑩ 步**统一应用/校验 |
| 宿主树（跑网关的 Hermes 源码） | 受管候选 v0.22.5 | **不变**（`HERMES_AGENT_DIR` 指过去；已是官方 v0.22.5 + 官方九件补丁） |
| 网关单元 | `hermes-gateway-default.service` | 不变（官方同名） |
| worker / memory 单元 | `superagent-worker-default.service` / `superagent-memory-default.service` | 不变（官方 `setup_worker.py` / `setup_memory.py` 生成，名字一致） |
| 受管 release 根 | 在役 | **冻结为只读回退底本**，不删除 |
| 嵌入服务 | `bge-m3-sidecar.service` → 127.0.0.1:8910 | 不变 |

## 4. 仓内改动

### 4.1 能力默认值补齐（能力全开的唯一来源仍是官方）

`install/plugin_defaults.py` 的 `defaults()` 增加两个插件：

- `markdown-delivery`：`enabled: true` + `engine_root` → 恢复 D05 文档交付工具面
  （`md_deliver` / `md_resolve`，全仓只有该插件提供）。
- `lan-cluster`：`enabled: true` + `engine_root` → 恢复 `lan_node_list/probe/rpc`
  工具面（无节点时是空集群，工具在位）。

`colleague-team` 官方默认已覆盖（`team_id` / `primary_id` / `registry_path`），
只因受管路径从不调用而没落地；切到官方链路后自然生效。

`local-model-grounding` **不加入官方默认**：它需要另一台机器上的
`scripts/ops/local_model_gateway.py`（仓库不提供该脚本，README 明确它在直连网线的
模型机上）与一个判别模型（本机 ollama 没有）。按 `RELEASE_CAPABILITIES.md` 第 10 节
「可选扩展与外部依赖」的口径，**逐项声明为关闭并写明原因**，不假装开启。

### 4.2 real-host 测试的宿主解析

上游 `8266014` 把宿主解释器查找从 `.venv/bin/python` 放宽为「`.venv` 没有就找
`venv`」，于是默认路径 `~/.hermes/hermes-agent`（已退役的 v0.22.2 旧树）从 SKIP
变成假红：`ImportError: cannot import name 'plugin_messages'`。

按仓库既有约定（`tests/test_frontdesk_boundary.py` 已在用
`SUPERAGENT_HOST_TEST_PYTHON`）统一解析顺序：

1. `SUPERAGENT_HOST_TEST_PYTHON`（显式指定）
2. `$HERMES_SOURCE/.venv/bin/python`
3. `$HERMES_SOURCE/venv/bin/python`

并且区分两种失败：

- **显式指定**宿主时宿主不合格 → 仍然 FAIL（不掩盖被指过去的坏宿主）
- **默认路径**下宿主版本不匹配 → 明确 SKIP 并打印原因（环境缺失，不算通过）

跑验证时显式指向实际运行宿主（受管候选源码 + Hermes 官方 PM 环境 venv 解释器），
这三条测试即对着真正在跑的宿主真跑真过。

## 5. 现场迁移步骤（每步可回退）

1. 备份：`~/.hermes` 全量 + `~/super_agent/default` 全量 → 带时间戳的只读目录
2. `HERMES_AGENT_DIR=<受管候选> bash install/install.sh --hermes-home ~/.hermes --dry-run`
   → 逐行核对计划，确认没有意外写盘
3. 真装：引擎 → `~/.hermes/agentcore`；插件 → 实体副本；官方默认能力写进
   `~/.hermes/config.yaml`；补丁步在候选树上应全部报「已在位」
4. 一次性迁移既有条目的 `engine_root` → `~/.hermes/agentcore`
   （官方默认「不覆盖已有 entry」，这属于部署方一次性迁移）
5. 官方 `install/setup_worker.py` / `install/setup_memory.py` 重新生成单元
6. 重启并验收：官方 `hermes tools list` / `plugins list`、真实消息通道用例、
   记忆召回、网关自报状态
7. 受管 release 根冻结为只读回退底本（不删）

## 6. 明确的取舍

- **失去 release 原子切换与失败自动回滚**：官方普通布局是幂等安装，中途失败会留下
  半装状态。缓解：冻结的受管 release 保留为完整回退底本。
- **插件升级要重跑 install.sh**：官方实体副本语义的必然结果。
- **受管布局（`tools/superagent.py` / `install/superagent.sh`）不再是 bot1 的主路径**：
  它是仓库官方提供的**另一种**布局，保留在仓里但不再承担本实例的部署职责。

## 7. 不做什么

- 不删 `~/super_agent/default`（留作回退底本）
- 不动 `bge-m3-sidecar` 与记忆库数据
- 不动 `~/.hermes/config.yaml` 里运维方选择（模型/provider/平台设置）
- 不消费历史 parked outbox（未知外部效果不盲重放）
- 不擅自下载判别模型来开 `local-model-grounding`
