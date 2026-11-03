# Super Agent 一键安装与整包升级

## 设计目标

Super Agent 的代码、技能、工具集、运行时资产、状态、备份和版本账本全部放在
一个可删除的根目录：

```text
~/.hermes/superagent/<profile>/
  current -> releases/<version>/
  releases/<version>/
  state/
  config/
  backups/
  install-manifest.json
```

Hermes 安装树**不再承载 Super Agent 的业务源码或卡片补丁**。Hermes 侧只保留
受管插件链接和 `config.yaml` 中的显式注册项；卸载时由 manifest 精确撤回。

## 日常命令

```bash
# 只读计划：写清版本、release 目录、旧卡片补丁、保留的主机补丁和阻塞项
./install/superagent.sh plan --profile <profile>

# 首次安装 / 同版本修复 / 新版本升级
./install/superagent.sh install --profile <profile> --apply

# 升级：先停本 profile 的 worker/memory/gateway，成功后启动；失败切回旧 release
./install/superagent.sh update --profile <profile> --apply

# 只读健康检查
./install/superagent.sh doctor --profile <profile>

# 切换或回滚到明确 release
python3 tools/superagent.py activate-release --profile <profile> --release <path>
python3 tools/superagent.py rollback --profile <profile>

# 完整卸载；默认删除受管根，插件链接与配置注册项按 manifest 撤回
./install/superagent.sh uninstall --profile <profile> --apply
```

`install` / `update` / `uninstall` 不带 `--apply` 时只打印计划，不写系统。

主机的 `identity/manifests/` 属于被 Git 忽略的现场配置，不会随干净 clone
或发布包自动出现。部署需要其中的服务环境设置时，显式提供同一份清单：

```bash
./install/superagent.sh update --profile <profile> --runtime-manifest /path/to/host-runtime.yaml --apply
```

显式清单不存在时，安装在修改前阻断。清单中的 `profile`（如有）必须与目标一致。
服务 drop-in 写入或 systemd reload 失败会报错；重试会再次 reload。
安装账本的 `runtime_configuration` 记录清单摘要和处理状态；`not_configured`
表示没有应用服务设置。这些状态不证明运行中的进程已加载环境，仍须在重启后核对。

## 干净 Hermes 候选

内部迁移先构建独立候选树，不在现役 Hermes 上逐块抠补丁：

```bash
python3 tools/superagent.py candidate-hermes --profile <profile>
```

候选必须来自精确的 Hermes v0.22.5 提交，并且只重放经过审阅的非卡片补丁（中文续跑、后台验证、记忆预取/保全、派工可见性），然后再次检查五项旧卡片补丁均不存在。候选树位于受管根内；失败不会改动现役 Hermes。

## 前台接待与后台同事

推荐路由（可用 `tools/plan_receptionist_routing.py` 先只读检查、再备份后应用）：

| 角色 | 路由 | 用途 |
| --- | --- | --- |
| 前台接待 | `flash` | 快速确认需求、分诊、建单、处理轻查询和杂活 |
| 后台 deep | `main` | 研发、诊断、代码修改和需要完整证据的任务 |
| 后台 architect | `pro` | 架构级规划与高难根因分析 |
| 忙时输入 | `steer` | 不把新需求长期堵在队列里，交接待员快速接管或改派 |

```bash
python3 tools/plan_receptionist_routing.py --profile <profile> \
  --profile-home <profile-home>
python3 tools/plan_receptionist_routing.py --profile <profile> \
  --profile-home <profile-home> --apply
```

该工具默认只读，使用 Hermes 官方 `config set`，应用前备份配置，且不会自动重启网关。

## 版本与回滚

- release 目录以 `git describe --tags --always --dirty` 命名；同一身份重装只会
  重建同一 release，不会覆盖其他版本目录。
- `current` 是指向 release 的单一符号链接，切换用原子 rename。
- 安装和激活前验证 `agentcore/liveness.py`、`plugins/`、`skills/` 和
  `release.json`；不完整的 release 不能成为 `current`。
- `install-manifest.json` 记录当前 release、最近 10 个 release、插件链接、配置
  注册项、树哈希和备份位置。
- `rollback` 只切换到 manifest 中仍完整的上一 release；不猜测目录。

## 旧卡片链路迁移

`plan` 会明确列出五项旧宿主卡片补丁和在位状态。迁移时：

1. 先快照 profile 的 `config.yaml` / `.env` 以及 Hermes 安装树的本地 diff。
2. 保留所有非卡片宿主补丁（中文续跑、后台验证、记忆预取/保全、派工可见性）。
3. 停止受影响服务，在候选 Hermes 树验证旧卡片补丁已移除。
4. 通过验收入口后才切换；失败恢复快照与旧 release。

候选生成已经自动拒用旧候选并固定到精确 Hermes 版本。现役树仍不会被原地逐块
抠补丁；只有候选树的五项旧卡片探针全部为 absent，才会写入 `adapter-is-clean`
标记。没有该标记时，实时 profile 上的五项旧补丁会作为阻塞项
`legacy_card_patches_require_clean_candidate`，不会静默双生产者上线。

候选树通过后，仍需在维护窗口完成源码切换、服务重启和真实消息验收；这些属于
运行时动作，不由 `plan` 或静态测试冒充已通过。

## 验收边界

- 单测只证明安装布局、幂等、配置合并、release 切换、回滚和卸载账本。
- 静态 plan / doctor 不是服务可用、工具注册或真实消息投递证明。
- 消息平台卡片必须另外经过真实测试机器人、客户端和投递安全回归。
- 现役 profile 在明确窗口与授权前不会被安装器重启或修改。
