# Super Agent v2.0 架构收敛与技术文档统一交付报告

编制日期：2026-11-08（Asia/Shanghai）  
基线版本：`98354f0` 及其关联演进  
验证状态：全套单元测试、门禁与干净室验证 100% 通过  

---

## 1. 架构演进与机制收敛概要

经过连续多轮的工程攻坚与单根架构升级，Super Agent 已完全收敛定型为 **v2.0 单根架构（Single-Root Architecture）**。本轮收尾工作针对技术文档体系进行了全面对账与规范化打磨，确保全套文档与生产运行源码 100% 吻合：

| 机制维度 | 收敛前状态 | v2.0 收敛后确定性实现 | 权威文档索引 |
| :--- | :--- | :--- | :--- |
| **部署根目录拓扑** | 散落多写者路径、旧版双根混杂 | 严格收敛于 `<managed_root>/<profile>`（默认 `~/super_agent/<profile>`），封装 `releases/<v>`、`current`、`config/`、`state/`、`workspaces/`、`identity/` 与 `manifest.json` | [`docs/ARCHITECTURE.md`](../../ARCHITECTURE.md)<br>[`docs/DEPLOY.md`](../../DEPLOY.md) |
| **插件与记忆提供方** | 历史零散记载 13 款 | 明确 **16 款受管插件**（13 通用内建 + 3 受管外部快照）与 **1 款独立向量记忆提供方**（`localvec`），严守两道启用门禁 | [`docs/PLUGINS.md`](../../PLUGINS.md) |
| **身份暂存与业务挂载** | 业务与通用身份边界模糊 | 通用骨架 `identity/SOUL.generic.md` 在受管发布中暂存，通过 `--compose-soul` 在部署层将 `{{BUSINESS_OVERLAY}}` 替换为业务私有规则 | [`docs/DEPLOY.md`](../../DEPLOY.md)<br>[`docs/INDEX.md`](../../INDEX.md) |
| **召回诊断观测器** | 仅有黑盒日志，缺乏状态沉淀 | `memory.localvec.diagnostic_path` 规范落位 `<managed_root>/state/recall-diagnostics/<profile>.json`，`superagent doctor` 自动化巡检与指标对账 | [`docs/TECH_MANUAL.md`](../../TECH_MANUAL.md)<br>[`docs/PLUGINS.md`](../../PLUGINS.md) |
| **宿主解释器推断** | 依赖手工指定或脆弱相对路径 | `tools/superagent.py` 实现四级自动推断链（显式参数 > `hermes-current` 软链 > `install-manifest.json` > 默认回退） | [`docs/DEPLOY.md`](../../DEPLOY.md) |
| **运维脚本布局感知** | 脚本硬编码假设受管路径 | `scripts/ops/_layout.sh` 引入纯函数布局解析（`managed` / `install.sh` / `explicit`），支持 systemd 模板单元对账与 `drift-backups/` 备份自愈 | [`docs/OPERATIONS.md`](../../OPERATIONS.md)<br>[`docs/superpowers/specs/2026-11-08-ops-scripts-layout-aware-design.md`](../specs/2026-11-08-ops-scripts-layout-aware-design.md) |
| **仓级业务中立性** | 易因文档说明泄漏业务词 | 仓级中立性棘轮门禁 `tests/test_repo_neutrality.py` 覆盖全量版本文件，零业务词、零内部指纹残留 | [`tests/test_repo_neutrality.py`](../../../tests/test_repo_neutrality.py) |

---

## 2. 文档体系打磨与对齐清单

本次打磨完成的文档矩阵：

1. **`README.md`**：更新插件统计为 16 款插件 + `localvec` 记忆提供方，建立清晰的技术文档导航入口。
2. **`docs/INDEX.md`**：新建权威文档总导航索引，划分核心规范、实践指南、活文档、专项规格与历史报告五大板块。
3. **`docs/TECH_MANUAL.md`**：全面同步至 v2.0 架构基线，补齐 16 款插件与同事工作区级联装配、召回诊断观测器集成（`recall_diagnostics_observation`）、单根受管拓扑与布局感知运维章节。
4. **`docs/ARCHITECTURE.md`**：系统性梳理十二层分层拓扑、单根受管结构图、8 大研发同事契约与 Worktree 锁机制、模型路由守护神 ADR-S1~S6。
5. **`docs/DEPLOY.md`**：规范化单根受管生命周期操作命令（plan/install/update/rollback/doctor/uninstall）、身份暂存挂载工作流与干净室验证门禁。
6. **`docs/PLUGINS.md`**：详尽枚举 16 款插件与 `localvec` 记忆提供方清单，明确 `settings.enabled` 总开关机制与同事 Profile 自动级联装配。
7. **`docs/OPERATIONS.md`**：详细阐述 `_layout.sh` 布局感知引擎、`uninstall-legacy.sh` 配置对账与 `drift-backups/` 安全自愈。
8. **`docs/superpowers/reports/`**：建立本专项收口闭环报告。

---

## 3. 深度验证记录

### 3.1 全仓单元测试与回归
- **执行命令**：`bash verify.sh`
- **测试结果**：**3103 passed, 46 skipped in 570.53s (9m 30s)**
- **失败项**：**0 failed**
- **跳过项说明**：46 项跳过全部为可选原生 C 绑定（`flowcoro_py`）或真实外网凭据依赖项，属于预期内环境隔离跳过。

### 3.2 仓级中立性棘轮门禁
- **执行命令**：`pytest tests/test_repo_neutrality.py`
- **测试结果**：**17 passed in 1.10s**，全仓无业务专有名词、无企业主机名、无内部内网网段泄漏。

### 3.3 部署链路硬门禁与端点检查
- DNS 解析延迟：`17.8ms`（预算：≤ 50ms）
- 幂等信箱 I/O 读写延迟：`0.18ms`（预算：≤ 20ms）
- 平台 OpenAPI 往返延迟：`117.4ms`（预算：≤ 800ms）
- 模型上下文端点契约：`1000000` (source=models)

---

## 4. 结论与后续演进建议

Super Agent v2.0 技术规范文档体系已全面定型收口，所有文档与代码保持单源真实（Single Source of Truth）。后续新增功能或插件时，必须严格遵守：
1. 始终通过 `superagent.sh` 统一入口执行计划与应用。
2. 任何文档或注释不得引入私有业务词，先跑 `pytest tests/test_repo_neutrality.py` 验证中立性。
3. 持续借助 `superagent doctor` 检验 `disk_bindings` 与 `recall_diagnostics_observation` 保证长效健康。
