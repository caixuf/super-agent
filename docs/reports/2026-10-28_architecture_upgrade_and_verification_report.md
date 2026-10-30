# 2026-10-28 · super-agent 架构升级与全量验证测试报告

## 1. 升级背景与核心动因

在当前多智能体协作与端到端自动化演进过程中，原系统存在以下三项核心缺陷：

1. **文档与代码严重脱节**：
   - 顶层文档（`README.md`、`identity/COLLEAGUE_MODEL.md`、`docs/DISPATCH_ANALYSIS.md`）仍停留在早期只读分析与单一任务池阶段；
   - 缺少对最新沉淀的 8 大官方研发同事模型（`architect`, `cases`, `reviewer`, `test`, `triage`, `backend`, `frontend`, `ops`）的规范定义；
   - 缺少对具备 `read, write, edit, bash` 工具的独立 Git Worktree 隔离运行器（`agentcore.dev_runner`）与跨进程文件锁（`fcntl.flock`）的说明；
   - 缺少对 11 款在用插件（`longtask_guard`, `durable_dispatch`, `colleague_team`, `markdown_delivery` 等）的完整架构落位与功能清单呈现；
   - 目录结构树严重滞后，遗漏大量新增核心模块。

2. **业务中立性断裂（门禁挂红）**：
   - 本仓最高设计规范要求通用内核完全业务中立（预留 `{{BUSINESS_OVERLAY}}` 插槽，由业务侧提供叠加层）；
   - 在近期合并的提交中，`agentcore/colleague_dev.py`、`agentcore/colleague_catalog.py` 及相关测试用例直接硬编码了<business-domain>与平台私有字面量，导致仓级业务中立性棘轮门禁 `tests/test_repo_neutrality.py` 与 `tests/test_business_neutrality.py` 持续断言失败。

3. **依赖导入边界违规**：
   - `agentcore` 作为通用底层 Python 包，其规范严禁静态依赖宿主或业务平台模块；
   - `colleague_dev.py` 与 `dev_runner.py` 中直接静态使用了 `from autotest_server.agent_flow import ...` 和 `from hermes_bridge.pi_executor import ...`，导致 `tests/test_import_boundary.py` 挂红。

---

## 2. 核心改动清单

### 2.1 业务中立与依赖解耦（底座层治理）

| 文件 | 变更性质 | 核心升级内容 |
| :--- | :--- | :--- |
| `agentcore/colleague_dev.py` | 架构解耦 | 1. 岗位职责与契约抽象为通用工程团队规范，剔除硬编码私有字面量；<br/>2. 平台工作区检测改为动态路径匹配机制（支持环境变量 `AUTOTEST_PLATFORM_ROOT` 与多目录扫描），消除静态绑定；<br/>3. 将静态引用改为 `importlib.import_module` 动态晚期绑定，杜绝依赖泄露。 |
| `agentcore/colleague_catalog.py` | 规范化 | 剔除注释与文档中残留的特定领域词汇，保持通用名录抽象。 |
| `agentcore/dev_runner.py` | 边界合规 | 1. 外部平台目录发现升级为 `resolve_platform_repo()` 动态解析；<br/>2. 外部执行器适配层改为 `importlib` 动态按需加载，彻底修复 `test_import_boundary.py` 违规。 |
| `tests/test_colleague_evidence.py` | 测试解耦 | 消除测试桩中的私有目录与字面量，改用通用测试路径与 `.dsl` 扩展名。 |
| `tests/test_dev_runner.py` | 测试解耦 | 测试断言对齐解耦后的标准岗位名称（`用例开发大师`），使用通用领域测试入参。 |
| `verify.sh` | 规范治理 | 清理终端回显中的 Emoji 符号，保持严谨的 `[PASS]` / `[FAIL]` 状态格式。 |

### 2.2 核心文档升级（代码与文档完全对齐）

| 文档路径 | 核心升级内容 |
| :--- | :--- |
| `README.md` | 1. 顶层定位与执行层：新增 `agentcore.dev_runner` 与 `agentcore.colleague_dev` 研发同事架构说明；<br/>2. 系统分层表：补齐研发同事执行层、11 款通用插件清单、通用技能库、运维审计工具集等；<br/>3. 全景目录树：从原先的简版记忆树扩展为覆盖 `agentcore/`, `plugins/`, `skills/`, `tools/`, `runtime/`, `patches/`, `install/`, `docker/` 的全量结构；<br/>4. 消除所有 Markdown 正文 Emoji 字符。 |
| `identity/COLLEAGUE_MODEL.md` | 1. 第 1 节：扩充为三层角色与研发同事团队，正式将 8 大专业研发岗位、Git Worktree 隔离与 `dev_runner` 纳入架构模型；<br/>2. 第 4 节：补充跨进程 `fcntl.flock` 文件锁互斥机制说明；<br/>3. 第 6 节：同步落地现状（研发隔离执行与团队插件已实装，动态跨 profile 进程拉起仍属过渡期）。 |
| `docs/DISPATCH_ANALYSIS.md` | 新增专门章节 `Development colleague runner (dev_runner)`，详细记录：<br/>1. 命令行调用规范与参数格式；<br/>2. Worktree 物理隔离与文件锁防并发冲突原理；<br/>3. 配备 `read, write, edit, bash` 的完整研发工具集与 bwrap 沙箱；<br/>4. `dev_traceability_v1` 证据协议与产物结构（`report.json`, `report.md`, `manifest.json`）；<br/>5. 与外部 SOP 微内核自愈流水线的衔接机制。 |

---

## 3. 全量测试与验证实测数据

### 3.1 仓级门禁总体执行

运行 `./verify.sh`（单一入口快检），结果如下：

```text
== ① 静态检查（shell 语法 + JSON/YAML 可解析）
  [PASS] 9 个 shell 脚本语法正确

== ② 单元测试（python3 -m pytest tests -q）
  [PASS] 1216 passed, 24 skipped in 199.63s (0:03:19)

== ③ 安装器 dry-run（零副作用）
  [PASS] 退出码 0

== 结果
  全部通过 [PASS]
```

- **静态语法检查**：9 个 Shell 脚本 100% 语法正确；
- **全仓单元测试**：**1216 个测试全部通过（1216 PASSED）**，24 个环境依赖用例正常 SKIP，**失败项为 0**；
- **安装器无副作用验证**：`install/install.sh --dry-run` 退出码为 0，零文件污染；
- **总体验收结论**：**全部通过 [PASS]**。

### 3.2 专项合规与门禁测试

| 门禁项 | 对应测试文件 | 校验内容 | 实测结果 |
| :--- | :--- | :--- | :--- |
| **仓级业务中立性棘轮** | `tests/test_repo_neutrality.py` | 拦截全仓可提交文件中的业务字面量 | **13 passed in 0.29s** |
| **包内业务中立性** | `tests/test_business_neutrality.py` | 拦截 `agentcore` 核心源码中的业务词汇 | **12 passed in 0.32s** |
| **依赖导入边界** | `tests/test_import_boundary.py` | 拦截宿主包与第三方包的静态侵入 | **5 passed in 0.17s** |
| **研发同事执行器** | `tests/test_dev_runner.py` | Worktree 隔离、文件锁互斥、产物落盘 | **6 passed in 1.34s** |
| **研发证据链协议** | `tests/test_colleague_evidence.py` | `dev_traceability_v1` 证据回执校验 | **7 passed in 0.60s** |
| **同事模型完整套件** | `tests/test_colleague*.py` | 会话管理、凭证流转、名录筛选、团队工具 | **241 passed, 2 skipped in 93s** |
| **安装器幂等与配置保护** | `tests/test_install_script.py` | 插件启用清单合并、密钥保护、重入幂等 | **10 passed in 8.78s** |

---

## 4. 本地真实网关与研发同事现地测试 (caixuf 高手 Profile)

利用本地现役的 `caixuf` profile（Hermes Gateway 运行实例）及底座调度框架进行真实联调验证：

### 4.1 网关与服务运行态探针
- **服务状态**：`hermes-gateway-caixuf.service` 状态为 `active (running)`，挂载 `10-card-24.conf` 与 `10-card-v2.conf` 平台补丁；
- **HTTP 探针**：访问 `http://127.0.0.1:8643/health`，返回 `{"status": "ok", "platform": "hermes-agent", "version": "0.22.5"}`，服务健康度正常；
- **插件工具集探针**：`durable_dispatch`、`markdown_delivery`、`write_guard`、`longtask_guard` 均已就绪且正常挂载。

### 4.2 研发同事编排器 (ColleagueDevOrchestrator) 联调测试
在隔离临时工作区运行 `ColleagueDevOrchestrator`，分角色实测回执：

```text
[测试 1] 研发同事名录提取：8 个官方研发角色完整加载 (architect, cases, reviewer, test, triage, backend, frontend, ops)。
[测试 2] 工作区自动搜寻：精准定位 <platform-root>。
[测试 3] 需求架构师 (architect)：输入自然语言 Story，成功黑盒拆解并原子落盘 contract_requirements.yaml (ok=True)。
[测试 4] 用例开发大师 (cases)：根据需求规格，成功逆向并生成领域 DSL 用例代码 (ok=True)。
[测试 5] 红队挑刺评审员 (reviewer)：针对测试用例执行静态时序与反模式扫描，给出 25.0 分并精确指出缺失退出断言与反向抑制 (ok=True)。
[测试 6] 仿真测试专家 (test)：静态 AST 语法门禁扫描，验证合法原语与原语守卫 (syntax_ok=True)。
```

全链路联调证明：解耦后的 `agentcore.colleague_dev` 既保持了底座中立性，又能无缝驱动上层微内核完成专业研发动作。

---

## 5. 符号与格式合规性检查

经自动化正则表达式扫描：
- 升级涉及的全部代码文件、配置文件、测试文件及 Markdown 文档中，**Unicode Emoji 字符数均为 0**；
- 状态标识统一采用 `[PASS]` / `[FAIL]` / `[说明]` 等工程化文本标记。

