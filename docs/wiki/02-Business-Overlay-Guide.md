# 02. 业务知识叠加与服务编排 (Business Overlay & Profile Services)

> **文档导航**：[Wiki 首页 (Home)](Home.md) │ [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) │ [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) │ [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) │ [04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)

---

## 1. 身份骨架合成机制 (Soul Skeleton Composition)

### 1.1 故障场景：业务规则硬编码与占位符污染
若将特定业务领域的专用状态枚举、系统接口或判定基准直接硬编码在通用智能体骨架内，底座代码将无法通用复用；而若使用松散的占位符（如包裹在 HTML 注释内），某些解析器或预处理器在清洗格式时会误删注释，导致业务提示词从未生效或引起格式错乱。

### 1.2 真实代码机制与注入命令
Super Agent 在 `identity/SOUL.generic.md` 第 9 节（第 254 行）保留了全仓唯一的**裸双花括号标记**：
```markdown
{{BUSINESS_OVERLAY}}
```
> [!IMPORTANT]
> 该标记严禁被 `<!-- ... -->` 等 HTML 注释包裹。安装器 `install/install.sh:662` 包含硬断言校验：
> `test "$(grep -cF '{{BUSINESS_OVERLAY}}' "$skeleton")" -eq 1`
> 必须恰好匹配 1 次，否则立即阻断退出。

业务团队可基于模板 `identity/overlays/domain_expert_overlay.template.md` 编写业务规则，并执行以下命令完成身份合成：
```bash
bash install/install.sh --profile <profile> --compose-soul /path/to/domain_overlay.md
```
底层脚本通过 `awk` 将 `{{BUSINESS_OVERLAY}}` 替换为业务规程内容，并直接落盘至 `<profile_home>/SOUL.md`。若原文件已存在，会自动备份为 `SOUL.md.bak-<timestamp>`。

### 1.3 代价与边界
身份合成是在部署期完成的静态落盘，业务规程更新需重新执行 `--compose-soul`；若通用骨架有多个标记或标记缺失，合成命令会强制拒绝以防止静默截断。

---

## 2. Profile Overlay 资产包机制 (`agentcore/profile_overlay.py`)

### 2.1 故障场景：多环境私有配置漂移与凭证泄露
企业各 Profile（如开发、预发、生产）常带有独立的 MCP 拓扑、私有记忆事实和访问凭证。若靠运维手工拷贝，容易遗漏敏感文件或出现路径硬编码导致迁移失败。

### 2.2 真实代码机制：打包与幂等合并
`agentcore/profile_overlay.py` 提供了资产打包（`collect_profile_overlay`）与幂等部署（`apply_profile_overlay`）：
1. **资产包结构与收集流程**：
   ```text
   overlay/
   ├── SOUL.md / AGENTS.md        # 角色与系统身份定义
   ├── memories/*.md              # 业务记忆片段（注入时追加 <!-- overlay import: <name> --> 标识）
   ├── skills/<skill_name>/       # 专有业务技能目录（包含 SKILL.md）
   ├── envs/*                     # 环境变量凭据（复制后强制设为 0600 权限）
   ├── config_snippet.json        # MCP 服务扩展配置
   └── metadata.json              # 资产包校验哈希与版本元数据
   ```
2. **MCP 配置自动展开合并**：
   `merge_mcp_into_profile_config()` 自动扫描 `config_snippet.json`，将配置中的 `{{PROFILE_HOME}}` 占位符动态替换为当前 Profile 的实际绝对路径，并安全合并至 `<profile_home>/config.yaml` 的 `mcp_servers` 节点。应用过程会自动备份原有 `config.yaml`（`.bak.<timestamp>`），支持多次幂等重入。

### 2.3 代价与边界
MCP 服务如依赖动态环境变量，建议通过独立的 `env_file` 注入并设置 `0600` 权限，避免敏感 Token 直接固化在 `config.yaml`。

---

## 3. 声明式多角色服务编排 (`agentcore/profile_spec.py`)

### 3.1 故障场景：多角色脚本各自启动导致生命周期失控
多智能体研发协作中，若前台网关与后台 8 大专家岗位由散落的终端命令分别启动，当主机重启或遭遇 OOM 时，缺乏依赖拓扑与自愈机制。

### 3.2 真实代码机制：规约解析与 systemd 单元渲染
通过 `agentcore/profile_spec.py` 中的 `BusinessProfileSpec`，管理员可声明完整的服务规约（包含 `profile_id`, `workers`, `proactive_hooks`, `memory`, `gateway`, `env`）：

```bash
# 1. 检查并验证 Profile 规约合法性
python3 -m install.setup_profile inspect /path/to/profile_spec.yaml

# 2. 渲染对应的 systemd user 服务单元文本
python3 -m install.setup_profile render /path/to/profile_spec.yaml --out-dir /tmp/rendered_units

# 3. 正式安装并激活受管服务单元
python3 -m install.setup_profile install /path/to/profile_spec.yaml --activate

# 4. 打包为便携式分发归档
python3 -m install.setup_profile pack /path/to/profile_spec.yaml --out /tmp/profile_bundle.tar.gz
```

**系统架构真实关系**：
- **1 个前台网关**：`hermes-gateway-<profile>.service`，负责对外连接协作平台并快速分诊；
- **8 个后台研发专家**：`superagent-worker-{profile_id}-{role}.service`，由 `DEV_ROLES` 派生，独立认领队列任务并在各自的 Git Worktree 中执行；
- **1 个长期记忆服务**：`superagent-memory-<profile>.service`。

### 3.3 代价与边界
为全部 8 个岗位分别拉起独立的后台 Worker 服务会占用系统进程与内存；资源受限时，可在规约中仅启用通用 Worker（`superagent-worker-<profile>.service`）串行承载所有角色。

---

## 4. 业务技能物理落盘机制 (Physical Skill Placement)

### 4.1 故障场景：虚拟环境变量断链
某些框架试图通过环境变量指向外部目录。一旦智能体切入沙箱环境、换用不同执行用户或被 systemd 重新拉起，环境变量丢失将导致技能静默失效。

### 4.2 真实代码机制：物理安装落盘
Super Agent 原生**不支持**任何虚拟路径映射表或外部环境变量，所有技能必须物理安装落盘于 `<profile_home>/skills/<skill>/`：

```bash
# 途径 1：通过专用工具批量物理安装
python3 tools/install_skills.py --profile <profile>

# 途径 2：通过受管安装脚本集成安装
INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC=/path/to/custom_skills bash install/install.sh --profile <profile>
```

每个技能目录内必须包含 `SKILL.md`，安装工具直接将技能复制到 `<profile_home>/skills/<skill_name>/SKILL.md`，并在部署体检时由 `plugin_links_valid` 校验。

### 4.3 代价与边界
物理复制意味着业务仓修改技能后，需重新执行安装命令更新；但彻底隔绝了宿主机路径变动与权限穿透引发的断链事故。

---

## 5. 延伸参考
- 角色权限矩阵与状态图引擎：[03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md)
- 生产环境配置与单根安装全流程：[`docs/DEPLOY.md`](../DEPLOY.md)
- 插件生命周期与挂载规范：[`docs/PLUGINS.md`](../PLUGINS.md)
