# 插件与工具面（16 款插件 + localvec 记忆提供方全景指南）

「智能体能做什么」由三层严格正交的架构决定，**严禁混为一谈**：
1. **插件（Plugins）**：向宿主注册底层工具集与拦截钩子。
2. **MCP 服务（业务工具面）**：通过标准化协议接入的外部业务系统，定义于业务叠加层。
3. **工具集注册（Toolset Registration）**：决定特定 profile / 角色会话能否看见并调用相应工具。

三者落位、生命周期与迁移口径各不相同。

---

## 1. 架构落位与责任归属

| 层 | 是什么 | 磁盘落位 | 责任归属 |
| :--- | :--- | :--- | :--- |
| **基础捆绑插件** | 宿主安装树自带插件（浏览器、系统调用、会话上下文引擎等） | `<hermes_root>/plugins/` | 宿主运行时自带 |
| **通用受管插件（13 款）** | 本仓原创的通用任务管理、调度、交付、集群协同与防护插件 | `plugins/<snake_name>/` → `<profile>/plugins/<kebab-name>/` | 本仓核心资产 |
| **外部受管插件（3 款）** | 生产验证的官方与第三方扩展（高级委派、遥测追踪、MCP 服务桥接） | `external-plugins/` → `<profile>/plugins/<name>/` | 受管快照发布 |
| **独立记忆提供方** | 向量化长期记忆后端（`localvec`，无 `plugin.yaml`） | `plugins/localvec/` → `<profile>/plugins/localvec/` | `memory.provider: localvec` |
| **MCP 服务（业务工具面）** | 外部业务系统的工具实现与 JSON-RPC 桥接 | profile `config.yaml` 的 `mcp_servers:`；实现体在业务仓 | **业务叠加层** |
| **工具集授权（Toolsets）** | 内置工具集、插件工具集与平台按需暴露列表 | `known_builtin_toolsets` / `known_plugin_toolsets` / `platform_toolsets` | 运行时配置基线 |

---

## 2. 16 款插件与 localvec 记忆提供方全清单

受管发布包含 **16 款标准插件** 与 **1 款独立记忆提供方**：

### 2.1 13 款本仓通用内建插件（带 `plugin.yaml`）

| # | 插件目录名 | 配置连字符名 | 核心功能与工具面 | 总开关与配置要求 |
| :-: | :--- | :--- | :--- | :--- |
| 1 | `capability_alert_outbox` | `capability-alert-outbox` | 能力退化与关键异常告警外发通道，向运维接收者投递结构化警报 | `entries.capability-alert-outbox.settings` |
| 2 | `colleague_catalog` | `colleague-catalog` | 提供只读研发岗位名册工具 `colleague_catalog`，供模型查看团队编制与边界 | `settings.enabled: true` |
| 3 | `colleague_team` | `colleague-team` | 研发同事生命周期治理（招聘、授权、退役）与网关派工原型 | `settings.enabled: true`（生产默认受控） |
| 4 | `durable_dispatch` | `durable-dispatch` | 后台持久队列派工（`queue_root`），提供任务提交、状态轮询与引用事实追溯 | `settings.enabled: true` + `allowed_roots` |
| 5 | `illustrated_delivery` | `illustrated-delivery` | 复杂图文与多媒体结构化文档渲染，支持 Headless 渲染排版 | `settings.enabled: true` |
| 6 | `lan_cluster` | `lan-cluster` | 局域网同构节点探测、心跳互保与 RPC 派工（`lan_node_list/probe/rpc`） | `settings.enabled: true` |
| 7 | `local_model_grounding` | `local-model-grounding` | 边缘本地轻量模型快筛与幻觉地线校验，拦截高频无害事实核验 | `settings.enabled: true`（依赖本地判别模型） |
| 8 | `longtask_guard` | `longtask-guard` | 前台长任务超时自动转入后台持久队列，防 HTTP/RPC 会话卡死 | `liveness_db` 配置，自动注册钩子 |
| 9 | `markdown_delivery` | `markdown-delivery` | 大体积技术文档直发、租户级分块与长文交付工具（`md_deliver` / `md_resolve`） | `settings.enabled: true` + `outbox_root` |
| 10 | `memory_bridge` | `memory-bridge` | 会话与长期记忆引擎的只读召回桥接，自动注入相关背景事实 | `settings.enabled: true` |
| 11 | `task_progress` | `task-progress` | 长任务中间进度流式广播与心跳保活反馈通道 | `settings.enabled: true` |
| 12 | `task_status` | `task-status` | 任务活体账本查询工具（`task_status`），返回精确耗时、心跳时延与日志尾 | `settings.enabled: true` + `liveness_db` |
| 13 | `write_guard` | `write_guard` | 工作区路径穿透防御与危险写操作拦截门禁，防止越界读写 | 默认挂载前置安全拦截钩子 |

### 2.2 3 款受管外部插件（Pinned External Plugins）

在受管安装流程中，通过 `tools/superagent.py` 的 `_snapshot_external_plugins` 从上游环境中固定快照并受管分发：

| # | 插件名 | 功能定位 | 生产管理原则 |
| :-: | :--- | :--- | :--- |
| 14 | `advanced-delegation` | 跨智能体上下文受控委派，限制子任务最大递归深度（Depth ≤ 1） | 严格遵守父子上下文隔离，不透传全局鉴权 |
| 15 | `langfuse` | 模型调用指标、Token 消耗、延迟与链路追踪（Telemetry）采集适配器 | 凭据由 `.env` 安全注入，禁止日志打印原始文本 |
| 16 | `mcp-serve` | 将宿主工具面转换为标准 MCP 服务协议向外暴露的连接桥 | 仅暴露经管理员白名单核准的工具 |

### 2.3 1 款核心记忆提供方（Memory Provider）

- **`localvec`**（位于 `plugins/localvec/`）：
  - **特性**：无 `plugin.yaml`（不作为常规宿主工具插件注册），属于 `memory.provider`。
  - **配置机制**：在 profile `config.yaml` 中声明 `memory.provider: localvec`。
  - **诊断路径契约**：在 `memory.localvec.diagnostic_path` 下指定诊断账本路径（默认 `<managed_root>/state/recall-diagnostics/<profile>.json`）。
  - **观测器集成**：内置召回诊断观测器（Recall Diagnostics Observer），在每次召回时记录检索延迟、Top-K 相似度分布、命中状态与缓存有效性，供 `doctor` 自动化体检。

---

## 3. 插件启用机制与「两道门禁」

### 3.1 启用列表与 `settings.enabled` 开关

很多插件在 `plugins.enabled` 中列出后**并不会直接对外暴露工具**。为防止配置未就绪时发生误调用，以下 9 款插件在 `register()` 入口实现硬判断：

```python
if not ctx.get_config("enabled", False):
    return  # 显式关闭或未配置时，静默返回，不注册工具面
```

必须在 `plugins.entries.<name>.settings` 中显式指定 `enabled: true` 才能真正激活：
- `colleague-catalog`
- `colleague-team`
- `durable-dispatch`
- `illustrated-delivery`
- `lan-cluster`
- `local-model-grounding`
- `markdown-delivery`
- `memory-bridge`
- `task-status`
- `task-progress`

### 3.2 验证生效的唯一铁律

> **「插件生效」的唯一充分证据：工具面列表（`tools list`）中能看到对应的函数名称。**
> 配置文件中存在字段或插件目录存在软链，绝不等于插件已成功加载。

---

## 4. 同事 Profile 自动装配与工作区隔离

在 v2.0 单根架构（`~/super_agent/<profile>`）中，当主 Profile 配置了多同事网关派工（`colleague-team` 的 `gateway_dispatch.tool_backend.members`）时，受管安装器（`tools/superagent.py`）自动执行**同事 Profile 级联装配**：

1. **工作区目录自愈**：为每个绑定的同事 Profile 自动创建 `<colleague_home>/workspace` 目录，确保隔离执行空间就绪。
2. **插件目录自动挂载**：确保 `<colleague_home>/plugins` 存在，并根据同事配置的 `plugins.enabled` 自动将当前受管 Release 的对应插件建立软链。
3. **记忆后端自动链接**：自动将 `localvec` 记忆提供方挂载至同事的 `plugins/localvec`，并将其 `engine_root` 锚定到主受管 Release 的 `current` 软链。
4. **双向诊断核对**：`_recall_diagnostics_observation` 扫描主实例及所有绑定同事的磁盘落位，输出 `disk_bindings` 对账状态（`matched` / `drifted`）。

---

## 5. 运维操作规范

1. **严禁手工篡改 YAML 缩进**：一律使用 `install/superagent.sh` 或官方 `install/install.sh` 脚本注入配置。
2. **敏感凭据外置**：插件所需外部 API 密钥统一存放在 `<profile_home>/.env`（权限 `0600`），严禁写入配置或代码仓。
3. **变更后必须重启服务**：插件更新或开关切换后，必须执行 `systemctl --user restart <service_name>`，并配合 `superagent doctor` 验证工具面完整性。
