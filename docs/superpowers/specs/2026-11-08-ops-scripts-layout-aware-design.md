# 官方运维脚本的布局感知改造（2026-11-08）

## 1. 问题

仓内 `scripts/ops/` 下两个脚本都以**受管布局**为唯一前提：

| 脚本 | 硬门槛 | 受管布局的前提 |
|---|---|---|
| `uninstall-legacy.sh` | `[[ -d "${CURRENT}" ]] \|\| 拒绝` | `${AGENTCORE_ROOT}/<profile>/current` 软链是引擎真相源 |
| `purge_and_rebuild_host.sh` | 同上 | 同上 |

其中 `CURRENT=${AGENTCORE_ROOT}/${PROFILE_NAME}/current`（`AGENTCORE_ROOT` 默认 `~/super_agent`）。

但本仓还提供**另一套官方布局**：官方 `install/install.sh --hermes-home <root>` 把引擎装到
`${HERMES_HOME}/agentcore`（安装器里的 `DEPLOY_DIR`），profile home 就是 `${HERMES_HOME}` 本身
（没有 `profiles/<name>` 子目录）。这套布局下：

- `${AGENTCORE_ROOT}/<profile>/current` **不存在** → 两个脚本直接 `exit 1`，
  报「受管 current 不存在，拒绝继续」，**一个检查都没做**；
- 于是本实例**没有官方漂移对账工具**：如果有人把某个 unit 的 `PYTHONPATH` 改成工作区源码，
  官方脚本不会报（它压根跑不起来）。

现场已存在这类单元：`agentcore-worker@.service` / `agentcore-memory@.service`
（模板单元，disabled）的 `PYTHONPATH` 指向工作区 `~/code/super-agent`，
而真正在跑的 profile 单元指向 `~/.hermes/agentcore`。

## 2. 目标与非目标

**目标**

1. 两个官方脚本在**受管布局**与**单写者布局**下都能跑，且各自保持原有语义。
2. 真相源的判定**只用一处规则**，与仓内 `agentcore/profile_spec.py:get_engine_root()` 同源，
   不在脚本里另发明一套。
3. **不放宽安全边界**：真相源确实不存在时，仍然拒绝继续。
4. 脚本改动**零手工配置**：不需要在实例上写死路径，全部走既有环境变量注入。

**非目标**

- 不改受管布局的既有行为（回归风险为零）。
- 不引入新的安装路径；单写者布局的「重建」仍然由**官方 `install/install.sh`** 写，
  不由脚本手工拼装路径（保持 `purge_and_rebuild_host.sh` 开头那句「重建复用受管安装器，
  不在本脚本手工拼装路径」的同一精神）。
- 不动在跑的 profile 单元 —— 它们本来就指向真相源，没有漂移。

## 3. 真相源解析模型

沿用 `agentcore/profile_spec.py` 的 `get_engine_root()` 顺序（显式 > 受管 `current`），
再补一条本仓 `install/install.sh` 自己造的引擎根：

```
1. AGENTCORE_ENGINE_ROOT（显式注入）        → 真相源 = 它，布局 = explicit
2. ${AGENTCORE_ROOT}/<profile>/current 存在  → 真相源 = 它，布局 = managed
3. ${HERMES_HOME}/agentcore 存在            → 真相源 = 它，布局 = install.sh
4. 都没有                                   → 拒绝继续（exit 1），沿用官方原话
```

判定顺序的理由：

- **显式优先**：实例部署清单要能覆盖一切（与 `profile_spec` 一致）。
- **受管 `current` 优先于 `${HERMES_HOME}/agentcore`**：两者可能同时存在
  （例如先用过受管布局、后来又跑了 `install.sh`），此时受管 `current` 是**有软链、
  有 manifest 校验**的发布物，语义更明确。脚本会把选中的真相源打印出来，不静默猜测。
- **`${HERMES_HOME}/agentcore` 兜底**：它是官方 `install/install.sh` 的 `DEPLOY_DIR`，
  本仓安装器自己造出来的目录，存在即代表这套布局在用。
- **都没有才拒绝**：官方那句「受管 current 不存在，拒绝继续」保护的是
  「拿一个不存在的路径去纠偏」；改成「真相源不存在，拒绝继续」后，这层保护完全保留。

## 4. 分支行为

### 4.1 `uninstall-legacy.sh`

| 步骤 | 受管布局 | 单写者布局 |
|---|---|---|
| 头 | `真相源=${CURRENT}` | `真相源=${HERMES_HOME}/agentcore` + `布局=install.sh` |
| [1/5] 服务代码来源 | 比对/纠偏到 `${CURRENT}` | 比对/纠偏到 `${HERMES_HOME}/agentcore`（同一段代码，只是真相源变量不同）→ 覆盖那对模板单元 |
| [2/5] 遗留 unit | 不变 | 不变 |
| [3/5] 两份配置 | `config.yaml` vs `profiles/<p>/config.yaml` | 单写者布局下 profile home **就是** `${HERMES_HOME}`，不存在第二份 → 明确报「单写者布局：只有一份配置，无二义」，**不报漂移** |
| [4/5] 散落 `proxy.env` | 扫 `${HERMES_HOME}/profiles` | 扫 `${HERMES_HOME}` 顶层（限深度 2，排除 `agentcore/`、`cache/`、`installs/` 等大目录） |
| [5/5] 验证 | 受管 `tools/superagent.py doctor` | 官方就绪门禁 `agentcore/tools/deployment_readiness.py --profile-home ${HERMES_HOME} --profile <p>` |

[1/5] 的纠偏逻辑保持不变：`PYTHONPATH=<工作区>` → `PYTHONPATH=<真相源>`，
`--apply` 前先 `backup_unit` 备份到 `drift-backups/`。**模板单元一并纳入**，
因为它们同样可能在将来被启用，指向工作区就是漂移。

### 4.2 `purge_and_rebuild_host.sh`

| 步骤 | 受管布局 | 单写者布局 |
|---|---|---|
| [1/4] 备份 | `profiles/<p>/memories`、业务技能目录、两份 `config.yaml` | `${HERMES_HOME}/memories`、`${HERMES_HOME}/skills`、`${HERMES_HOME}/config.yaml` |
| [2/4] 下线遗留服务 | 不变 | 不变 |
| [3/4] 归档散落配置与运行时状态 | `profiles/<p>/proxy.env` 等 | `${HERMES_HOME}/proxy.env`（存在才归档） |
| [4/4] 重建与验证 | `tools/superagent.py update` + `doctor` | 官方 `install/install.sh --hermes-home ${HERMES_HOME}` + 官方就绪门禁 |

单写者布局下的重建调用**官方 `install.sh`**，正是「单一写者」原则本身：
脚本不自己拼装引擎目录，而是把重建交回官方安装器。

## 5. 边界与失败模式

| 情况 | 行为 |
|---|---|
| 真相源不存在 | 拒绝继续（exit 1），提示语从「受管 current 不存在」改为「真相源不存在」 |
| `AGENTCORE_ENGINE_ROOT` 指向不存在的路径 | 拒绝继续（显式注入不许兜底，避免掩盖配置错误） |
| systemd 用户目录不存在 | [1/5] 跳过（沿用官方 `[[ -d "${SYSTEMD_DIR}" ]]` 判断） |
| `--apply` 改写 unit | 先备份到 `drift-backups/backup_<ts>/`，可 `--rollback` |
| 脚本不重启服务 | 保持官方语义（`--apply` 后提示需手动 restart） |

## 6. 测试

新增 `tests/test_ops_scripts.py`，用**临时 HOME 沙箱**跑脚本（绝不动真实现场）：

1. 真相源解析三种情形：受管布局 / 单写者布局 / 都没有 → 拒绝（exit 1）。
2. 显式 `AGENTCORE_ENGINE_ROOT` 覆盖前两者。
3. 漂移识别：造一个 `PYTHONPATH` 指向工作区的 unit → report 模式报 `[DRIFT]`。
4. `--apply` 把该 unit 改写成真相源，且备份目录里有原件。
5. 单写者布局下 [3/5] 不误报「两份 config.yaml 内容不同」。
6. 单写者布局下 [5/5] 调用的是就绪门禁而不是受管 `doctor`。
7. 受管布局下 [3/5] 仍会检出「两份配置不同」（回归护栏）。

## 7. 文档

- 本设计文档（仓内 `docs/superpowers/specs/`）。
- `docs/reports/2026-11-07-official-path-deployment.md` 追加 §17：改造记录 + 实机 `--apply` 结果；
  同时**更正 §15.2**：`~/.hermes/profiles/cli_profile` 不是静态旧版本残留，
  它是 Hermes CLI 每次运行都会重建的**临时 profile**（`hermes_cli/main.py` 的
  `explicit_cli_profile`），删掉会再生 —— 这一点原记录写错了。
