# 01. 单根受管部署与极速上手 (Quickstart & Single-Root Deployment)

> **文档导航**：[Wiki 首页 (Home)](Home.md) │ [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) │ [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) │ [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) │ [04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)

---

## 1. 单根受管拓扑与状态持久化架构

### 1.1 传统部署故障场景
在多实例智能体部署中，若代码、配置、状态和 systemd 服务散落在 `~/.local/`、`/etc/` 等不同系统目录中，会引发以下严重故障：
1. **升级状态踩踏**：拉取新代码时覆盖正在执行长任务的工作区，破坏运行时上下文；
2. **回滚不可逆**：新版本报错时，旧版本镜像已被覆盖，无法原子回退；
3. **数据相互踩踏**：多个 Profile 共用顶层数据库，导致任务队列与活体心跳混杂。

### 1.2 单根目录拓扑与代码机制
Super Agent 严格将单个实例的所有资产封装在 `<root>/<profile>` 目录下（默认 `~/super_agent/<profile>`，由 `tools/superagent.py` 统一定义）：

```text
~/super_agent/<profile>/
├── current -> releases/<git-describe-or-sha>/  # 原子软链：指向当前生效的不可变发布镜像
├── releases/                                   # 不可变历史版本库（按 tag/commit 命名，非时间戳）
│   ├── v2.2.1/                                 # 历史稳定版本（秒级回滚锚点）
│   └── v2.2.2-single-root-20261111-dirty/      # 当前激活版本代码、内置插件与核心工具
├── state/                                      # 运行时持久状态（跨版本升级完整继承）
│   ├── analysis-queue/pool.db                  # 调度分析任务池 SQLite WAL 库与文件锁
│   ├── dev-queue/pool.db                       # 研发专家任务池 SQLite WAL 库与文件锁
│   ├── liveness.db                             # 活体心跳数据库（维护 task_liveness 表）
│   ├── memory-spool/                           # 长期记忆事件池（注意连字符）
│   ├── cards/                                  # 交互卡片状态与事件存储根目录
│   ├── markdown-outbox/                        # 离线 Markdown 消息与卡片发件箱
│   ├── colleague-team/registry.sqlite          # 团队成员注册表
│   └── recall-diagnostics/                     # 记忆召回指标 JSON 存放目录
├── config/                                     # 实例配置快照（config.yaml 等）
├── backups/                                    # 自动快照备份仓（升级前自动归档）
└── install-manifest.json                       # 安装清单：记录 commit、插件软链与托管服务
```

### 1.3 代价与边界
`state/` 跨版本保留保证任务连续性，但要求数据库 schema 保持向前兼容；不可变 release 随升级累积占用磁盘，需定期通过 `prune-releases` 安全回收。

---

## 2. 从零部署真机实测（沙箱零副作用演练）

以下演练基于 `/tmp/sa_walkthrough_probe` 临时沙箱，全程零外部依赖，不改动宿主真实配置。

### 步骤 0：准备临时沙箱骨架
```bash
rm -rf /tmp/sa_walkthrough_probe
mkdir -p /tmp/sa_walkthrough_probe/profile_home /tmp/sa_walkthrough_probe/hermes/plugins/platforms/feishu
printf 'platforms: {}\n' > /tmp/sa_walkthrough_probe/profile_home/config.yaml
printf '# probe dummy adapter\n' > /tmp/sa_walkthrough_probe/hermes/plugins/platforms/feishu/adapter.py
```

### 步骤 1：安装前诊断未就绪状态
```bash
python3 tools/superagent.py \
  --root /tmp/sa_walkthrough_probe/super_agent \
  --profile probe \
  --profile-home /tmp/sa_walkthrough_probe/profile_home \
  --hermes-root /tmp/sa_walkthrough_probe/hermes \
  doctor
```
- **退出码**：`1`
- **真实 JSON 输出**：`{"status": "not-installed", "root": "/tmp/sa_walkthrough_probe/super_agent/probe"}`

### 步骤 2：预览部署计划 (Dry-run Plan)
```bash
python3 tools/superagent.py \
  --root /tmp/sa_walkthrough_probe/super_agent \
  --profile probe \
  --profile-home /tmp/sa_walkthrough_probe/profile_home \
  --hermes-root /tmp/sa_walkthrough_probe/hermes \
  plan
```
输出结构化 JSON：包含 `layout`、`managed_paths`、13 款内建插件与 `localvec` 记忆提供方的软链规划，退出码 `0`。

### 步骤 3：执行正式安装
```bash
python3 tools/superagent.py \
  --root /tmp/sa_walkthrough_probe/super_agent \
  --profile probe \
  --profile-home /tmp/sa_walkthrough_probe/profile_home \
  --hermes-root /tmp/sa_walkthrough_probe/hermes \
  --allow-dirty \
  install
```
- **退出码**：`0`
- **执行动作**：创建 `releases/<version>` 不可变目录、原子链接 `current`、初始化 `state/` 队列与数据库、建立插件软链并生成 `install-manifest.json`。

### 步骤 4：安装后健康体检
```bash
python3 tools/superagent.py \
  --root /tmp/sa_walkthrough_probe/super_agent \
  --profile probe \
  --profile-home /tmp/sa_walkthrough_probe/profile_home \
  --hermes-root /tmp/sa_walkthrough_probe/hermes \
  doctor
```
输出包含 17 项观测键的 JSON 字典。在开发脏工作区安装时，`release_valid: true`, `plugin_links_valid: true`, `release_hash_valid: true` 均为 `true`，因检测到脏提交返回 `"status": "drift"` 与 `"reasons": ["installed_dirty"]`（退出码 1）。

---

## 3. systemd 服务单元管理

### 3.1 真实服务单元命名规范
每个受管 Profile 拥有独立命名的 systemd 用户服务单元，避免命名冲突：
- **前台网关服务**：`hermes-gateway-<profile>.service`（由 `tools/superagent.py` 与网关安装器管理）；
- **通用任务 Worker**：`superagent-worker-<profile>.service`（`setup_worker.py` 生成）；
- **角色专用 Worker**：`superagent-worker-{profile_id}-{role}.service`（按岗位规格由 `profile_spec.py` 编排）；
- **长期记忆运行时**：`superagent-memory-<profile>.service`（`setup_memory.py` 生成）。

### 3.2 服务管理命令
宿主开启常驻守护（用户注销后后台服务不终止）：
```bash
loginctl enable-linger $USER
```
管理实例服务：
```bash
# 查看网关与 Worker 状态
systemctl --user status hermes-gateway-<profile>.service superagent-worker-<profile>.service

# 启动与跟踪日志
systemctl --user start hermes-gateway-<profile>.service
journalctl --user -u superagent-worker-<profile>.service -f
```

---

## 4. 版本升级、回滚与清理操作

所有维护命令均通过统一入口执行，严格遵循参数规范：

```bash
# 1. 平滑升级：构建新 release、原子切换 current 软链接、平滑重启服务
python3 tools/superagent.py update --profile <profile>
# 或通过 Shell 包装器（外层必须带 --apply）
./install/superagent.sh update --profile <profile> --apply

# 2. 秒级回滚：将 current 切换回 manifest 历史中的上一个 release（无 --apply）
python3 tools/superagent.py rollback --profile <profile>

# 3. 历史版本清理：保留最近 3 个 release，安全删除陈旧目录（无 --apply）
python3 tools/superagent.py prune-releases --profile <profile> --keep 3

# 4. 安全卸载：注销服务、清理软链接并归档配置
./install/superagent.sh uninstall --profile <profile> --apply
```

> [!NOTE]
> `rollback` 与 `prune-releases` 属于原生 CLI 子命令，参数解析中不包含 `--apply`；追加 `--apply` 会报错。`prune-releases` 始终自动保护当前激活的 `current` 版本与最近备份。
