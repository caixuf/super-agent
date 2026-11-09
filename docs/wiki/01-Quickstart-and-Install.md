# 01. 5分钟快速上手与单根部署 (Quickstart & Single-Root Deployment)

> 📚 **导航 / Wiki Navigation**
> [🏠 Wiki 首页 (Home)](Home.md) │ [🚀 01. 5分钟快速上手与单根部署](01-Quickstart-and-Install.md) │ [🧩 02. 业务知识挂载指南](02-Business-Overlay-Guide.md) │ [🤖 03. 前后台协同与 StateGraph](03-Multi-Agent-and-StateGraph.md) │ [📨 04. 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [🛠️ 05. 运维排障与 FAQ](05-Operations-and-Troubleshooting.md)

---

## 1. 为什么采用单根部署架构 (Single-Root Architecture)？

在传统智能体部署方案中，代码、配置、运行时状态、任务队列与 systemd 服务往往散落在系统的各个角落（如 `~/.local/`, `~/.config/`, `/etc/`, `~/.cache/` 等）。这种分散拓扑会引发一系列严重维护难题：
1. **升级状态踩踏**：升级新版本代码时，正在运行的长任务被直接覆写，导致状态破坏；
2. **回滚极其困难**：无法精确获知上次稳定版本的完整目录镜像，出现故障只能人工盲目修补；
3. **残留难以清除**：卸载或重置时遗漏孤儿软链接或废弃配置，持续干扰后续实例；
4. **多实例隔离脆弱**：多套环境（如开发态、仿真态、生产态）互相读取彼此状态数据库。

为了彻底根除上述隐患，Super Agent 确立了**单根受管目录铁律**：每一个智能体实例（Profile）的全部生命周期资产，严格封装在独立唯一的受管根目录下：

```text
~/super_agent/<profile>/
├── current -> releases/20261109_140000/    # 原子软链接：指向当前生效的不可变版本
├── releases/                               # 版本仓库：历史版本物理隔离存放
│   ├── 20261108_100000/                    # 旧版本镜像（可用于秒级回滚）
│   └── 20261109_140000/                    # 当前激活版本代码、工具与通用技能
├── state/                                  # 运行时持久状态（跨版本继承）
│   ├── durable_pool.sqlite                 # SQLite 持久任务池数据库（WAL 模式）
│   ├── liveness_ledgers/                   # 任务活体心跳账本与 PID 证据
│   └── memory_spool/                       # 长期记忆事件池与向量索引
├── config/                                 # 受管配置快照
│   └── config.yaml                         # 当前实例生效配置
├── backups/                                # 自动备份仓（每次升级/变更前自动快照）
└── install-manifest.json                   # 安装清单：记录当前 release、插件链接与受管服务
```

---

## 2. 环境先决条件 (Prerequisites)

在开始部署前，请确保宿主环境满足以下条件：

- **操作系统**：Linux x86_64 或 aarch64（支持 Ubuntu 20.04+、Debian 11+、WSL2）；
- **Python 版本**：Python 3.10+（强烈推荐 Python 3.11 或 3.12）；
- **基础依赖**：Git、curl 或 wget、build-essential；
- **用户级服务管理 (systemd user session)**：
  确保当前用户已启用进程常驻（用户注销后后台服务不被系统收回）：
  ```bash
  loginctl enable-linger $USER
  ```
  通过以下命令检查 user session 是否正常：
  ```bash
  systemctl --user status
  ```

---

## 3. 5 分钟极速安装实战 (Step-by-Step)

### 第一步：获取代码与本地快速自检

首先克隆代码仓库，并执行轻量级静态门禁自检：

```bash
git clone <repo_url> super-agent
cd super-agent

# 执行全套静态门禁与核心单测（耗时约 30 秒，零外部网络依赖）
bash verify.sh
```

若输出提示 `ALL GATES PASSED`，则表明当前代码与环境满足安全基线。

---

### 第二步：预览安装计划 (Dry-run Plan)

Super Agent 遵循**“先看计划、再行修改”**的确定性安全哲学。执行 `plan` 命令仅计算预期的目录变更与服务注册，不会对操作系统产生任何副作用：

```bash
# 规划名为 dev 的 profile 安装
./install/superagent.sh plan --profile dev
```

终端将打印结构化的安装执行计划：
- 目标单根路径：`~/super_agent/dev`
- 准备创建的目录拓扑与版本归档名
- 拟安装的通用插件列表与软链接映射
- 拟生成的 systemd user 单元

---

### 第三步：正式应用安装 (Apply Installation)

确认计划无误后，添加 `--apply` 开关执行原子安装：

```bash
./install/superagent.sh install --profile dev --apply
```

安装器将自动化执行：
1. 编译并打包不可变 release 归档，存放于 `~/super_agent/dev/releases/<timestamp>/`；
2. 原子建立 `current` 软链接指向该 release；
3. 初始化 `state/` 下的持久任务池与账本数据库；
4. 配置并链接受管插件；
5. 生成 `super-agent@dev.service` 用户单元，并执行 `daemon-reload`。

---

### 第四步：全局健康体检 (Doctor Diagnostics)

安装完成后，运行 `doctor` 命令对新建实例进行全面的端到端健康体检：

```bash
python3 tools/superagent.py doctor --profile dev
```

`doctor` 工具将逐项检查：
- [x] 受管根目录与原子链接完整性（`current` 是否有效链接）；
- [x] 持久任务池数据库（SQLite WAL 完整性检查与读写测试）；
- [x] systemd 用户服务状态与存活心跳；
- [x] 插件软链接与依赖项完备性；
- [x] 权限边界与隔离策略。

体检结果若全部显示 `[PASS]`，则代表系统已处于就绪状态。

---

### 第五步：启动服务与查看日志

通过标准 systemctl 命令管理当前实例：

```bash
# 启动常驻服务
systemctl --user start super-agent@dev.service

# 查看服务运行状态
systemctl --user status super-agent@dev.service

# 跟踪查看实时运行日志
journalctl --user -u super-agent@dev.service -f
```

---

## 4. 日常生命周期管理 (Lifecycle Operations)

### 1. 无缝平滑升级 (Update)

当代码仓库有新版本发布时，使用 `update` 命令执行平滑升级：

```bash
python3 tools/superagent.py update --profile dev --apply
```

升级机制采用**无损接管**：
- 新代码打包部署至新的 `releases/<new_timestamp>` 目录；
- 旧的持久任务池与未完成任务完整保留在 `state/`；
- 原子切换 `current` 软链接；
- 平滑重启后台 Worker 服务，未完成任务由新的 Worker 自动恢复执行。

### 2. 秒级紧急回滚 (Rollback)

若新版本在运行时出现非预期异常，可通过 `rollback` 命令实现秒级一键回退：

```bash
python3 tools/superagent.py rollback --profile dev
```

回滚机制会将 `current` 软链接立刻指向前一个稳定的 release 目录，并重启服务，无需重新编译或从云端拉取。

### 3. 历史版本清理 (Prune Releases)

随着持续迭代，`releases/` 目录下会沉淀多个历史版本。使用 `prune-releases` 命令可安全回收磁盘空间：

```bash
# 保留最近 3 个 release，清理更早的历史版本
python3 tools/superagent.py prune-releases --profile dev --keep 3 --apply
```

`prune-releases` 会主动保护当前激活的 `current` 版本以及上一次稳定备份，绝不误删在用资源。

### 4. 彻底干净卸载 (Uninstall)

若需要完全销毁实例：

```bash
python3 tools/superagent.py uninstall --profile dev --apply
```

卸载工具会停止并注销关联的 systemd 用户服务，安全清理插件软链接，并移除 `~/super_agent/dev` 受管目录。

---

## 5. 自动化冒烟与容器验证 (Verification & Cleanroom)

- **本地轻量级冒烟探针**：
  ```bash
  python3 checks/prefer_d7_smoke.py
  ```
  在本地环境对任务提交、卡片排队与状态流转进行轻量级闭环冒烟。

- **Docker 纯净环境验证 (Cleanroom Test)**：
  ```bash
  bash scripts/ops/docker_cleanroom_test.sh
  ```
  该脚本会在全新的轻量 Linux 容器环境中从零模拟全新主机的完整安装、服务启动、健康检查与卸载流程，执行 20 项断言，确保代码在任何未被污染的宿主机上均可稳定复现。

---

> ➡️ **下一步**：单根环境就绪后，请阅读 [02. 业务知识挂载指南](02-Business-Overlay-Guide.md) 了解如何零污染接入业务 MCP、Skills 与领域专家角色。
