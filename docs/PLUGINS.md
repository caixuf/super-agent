# 插件与工具面（插件 / MCP / 工具集注册）

「机器人会做什么」有三层，**别混为一谈**：插件提供能力，MCP 提供**业务工具面**，工具集注册决定
**谁看得见哪些工具**。三者各自的迁移动作不同，漏掉的表现也不同。

## 1. 三层分别住在哪

| 层 | 是什么 | 住哪 | 归谁 |
| :--- | :--- | :--- | :--- |
| **随发行版捆绑的插件** | Hermes 安装树自带（浏览器、计算机操作、上下文引擎、cron 提供方、认证、委派、磁盘清理、会议……） | `<install>/plugins/` | 通用；随升级走 |
| **插件安装副本 + 启用记录** | 第三方 / 官方扩展的实际副本 **按 profile 各一份**；启用写在 profile 配置的 `plugins:` 段 | `$HERMES_HOME/profiles/<p>/plugins/` + `config.yaml` 的 `plugins:` | 通用；**启用属运行时基线** |
| **MCP 服务（业务工具面）** | 外部工具实现 + 注册项 | 注册在 profile 配置的 `mcp_servers:`；实现体在业务仓 | **业务叠加层**（工具名就是业务名词） |
| **工具集注册** | 内置工具集、插件带来的工具集、按平台裁剪的工具集 | 配置里的 `known_builtin_toolsets` / `known_plugin_toolsets` / `platform_toolsets` | 通用 |

## 2. 只走官方入口

- **不要手编** `plugins:` 或 `mcp_servers:` —— 与现场配置同源，一个缩进就能把网关弄坏。
  用官方的连接 / 安装入口去装、去授权：`hermes plugins enable <name>`。
- 唯一例外是**安装器的启用记录与缺失安全默认项**（`install/install.sh` 第 ⑧ 步；全新 profile 自动回退，
  也可 `PLUGIN_ENABLE_MODE=merge` 强制）：它只增删 `plugins.enabled` / `plugins.disabled`
  两个列表的行；另一个小型文本合并器只为**缺失**的 `plugins.entries` 写安全默认项。
  已有 entry、关闭项、其它键、注释与内联密钥保留；结构超出可证明范围会拒绝写入。
  默认值只包括本仓引擎路径、profile 本地账本/登记库路径和只读分析岗位；`queue_root`、
  `allowed_roots`、凭据等部署值仍由部署方提供。
- **启用后要重启网关（或由官方 `hermes plugins enable` 触发的插件热重载）才生效**。判定
  「是否真生效」的唯一可靠方法是**看工具面里出现了该工具**，
  不是看配置文件里有没有那一行（写了但没起效是最常见的假绿）。
- MCP 服务拉起来后，**核对它的工作目录指向部署位**：否则会出现「改的是仓库、跑的是旧拷贝」，
  表现为"代码明明改了但行为没变"。

## 3. 迁移清单里必须逐项点名（全在 git 之外）

1. 每个 profile 的**插件副本与启用记录**（`profiles/<p>/plugins/` + `plugins:` 段）；
2. **MCP 注册项**及其实现体的部署位；
3. MCP / 插件所需的**凭据**（`.env`，**永不打包**，只给键名清单）；
4. **工具集注册**（`known_plugin_toolsets` 等）—— 换机器后插件副本不在，这些名字会对不上；
5. **子 agent 的工具面继承开关**（决定派工出去的子会话看不看得见工具）。

## 4. 本仓做什么、不做什么

- **做**：执行器适配器（把长任务派出去跑的通用件）、角色契约里那条硬纪律
  ——「派工时必须把角色契约全文内联进 context，子 agent 不知道父会话的任何事」。
- **做**：`install/install.sh` 把 `plugins/<snake_case>/` 以连字符名落位到
  `<profile>/plugins/`，并把带 `plugin.yaml` 的插件名写进该 profile config 的
  `plugins.enabled`，但保留 `plugins.disabled`、`entry.enabled: false` 与
  `settings.enabled: false` 的显式关闭；只给缺失 entry 补安全默认值。`--skip-plugins` 两步都跳过。
  入口优先级：目标 profile 已能被官方命令寻址 → `hermes plugins enable <name>`；
  全新 profile / 无 CLI → 最小文本合并（见第 2 节）。
- **不做**：任何业务工具名、业务 MCP 服务本体、业务插件、业务凭据键名 ——
  这些统一由业务叠加层的部署清单给出。`plugins/localvec` 没有 manifest，
  它是 `memory.provider`（配 `memory.provider: localvec` + `recall_cmd`），不在启用列表里。

> 验收口径（一句话）：**「插件装上了」= 工具面里看得见它；「MCP 配好了」= 工具调用真返回业务数据**。
> 配置文件里的那一行不算证据。

## 5. 带 `settings.enabled` 总开关的插件（"看着启用"与"真的启用"）

下面这些插件**即使名字进了 `plugins.enabled` 也还不生效** —— `register()` 第一行就检查
`plugins.entries.<name>.settings.enabled: true`，缺了就静默 return（不报错、不注册工具）：

`colleague-catalog` · `colleague-team` · `durable-dispatch` · `illustrated-delivery` ·
`local-model-grounding` · `markdown-delivery` · `memory-bridge` · `task-status` · `task-progress`

`install/install.sh` 的 ⑧ 步会把这份名单打出来，并仅为缺失 entry 设置安全默认值；
`--require-ready` 可追加只读门禁，但静态配置齐全也不代表运行时工具注册或端到端投递已验证。

## 6. 同事类插件的当前就绪度（两件，别混）

| 插件 | 状态 | 说明 |
| :--- | :--- | :--- |
| `colleague-catalog` | **可生产启用** | 只读名册：把管理员配置的岗位（`display_name`/`duty`/`can_do`/`boundaries`）暴露成 `colleague_catalog` 工具。**不授权、不派工、不落队列**；角色 id 必须与派工插件里的角色对得上，否则派工仍然会拒。 |
| `colleague-team` | **未接驱动，勿在现有系统启用** | 登记库（`agentcore/colleague_registry.py`）与 `colleague_list/define_role/hire/grant/retire` 工具都已实现，但**实例创建、岗位派工、最终消息发送、生产停用四条驱动尚未接上**（见 `plugins/colleague_team/README.md` 的「当前边界」）。启用只能记账，**不要对外宣称"招聘完成/成员在岗"**。 |

要真的让同事干活，当前可用的两条链是：**durable-dispatch 名册**（只读分析岗，按 `queue_root` +
worker 单元跑）与 **`delegate_task` 子 agent**（一次性叶子工人，深度 1）。
回执能不能回到会话见 `docs/COMPLETION_DELIVERY.md`。
