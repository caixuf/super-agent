# M1 可信执行后端技术方案评估报告

- 评估日期：2026-11-09（Asia/Shanghai）
- 依据规范：[`TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md`](../TRUSTED_AUTONOMOUS_EXECUTION_PLAN.md) 第 3 节 M1 与第 4 节
- 宿主环境：Linux 5.15+ x86_64, Ubuntu 24.04 LTS, Python 3.12.3
- 状态：**已完成评估、选型落地与实测验收（`AttemptBoundSandboxAdapter` 已在 `agentcore/colleague_containment.py`、`agentcore/attempt_supervisor.py`、`agentcore/colleague_worker.py` 与 `agentcore/dev_runner.py` 落地并通过 27 项 M0/M1 专项回归验证）。**

---

## 1. 评估背景与核心安全需求

M1 里程碑的核心目标是：**证明任务从启动起就在受控边界内，而不是停止时才靠名称或环境变量猜测归属**。
执行后端必须满足以下五大硬性约束：
1. **全后代确定性停止（Deterministic Stop）**：任务取消或超时时，彻底终结包括 `setsid`、`double-fork` 在内的所有派生子进程，不留孤儿僵尸进程。
2. **凭据与敏感路径隔离（Filesystem & Credential Boundary）**：禁止访问宿主凭据（`~/.ssh`, `~/.config`, 生产 Profile 配置）和控制套接字（Docker sock, Systemd D-Bus），仅开放受权工作树可写。
3. **网络访问策略约束（Network Policy）**：支持离线禁用外网访问（避免数据外发或意外调用未授权外部服务）。
4. **资源与预算控制（Resource Budgets）**：执行时间（Timeout）、进程数（Tasks）、内存（Memory）受控，不可无限消耗宿主资源。
5. **最小特权与低延迟（Rootless & Low Overhead）**：无需 root 特权，不暴露高危特权后门，启动开销小（适合高频短任务）。

---

## 2. 候选后端实机测试与能力对比

在当前真实机器上针对三个候选技术进行了命令实测与能力核对：

### 2.1 候选 A：`systemd-run --user` 瞬态单元（Transient Units）

- **实机可用性验证**：已安装 `/usr/bin/systemd-run`，当前用户 Systemd 守护进程（PID 497）正常运行。
- **实测通过项**：
  1. **瞬态单元生命周期**：`systemd-run --user --unit=<name> ...` 自动在 `/user.slice/user-1000.slice/user@1000.service/app.slice/` 下建立隔离 cgroup。
  2. **cgroup Stop 语义（全后代强杀）**：启动通过 `start_new_session=True` 脱离会话的 `sleep 120` 后代，执行 `systemctl --user stop <unit>`，cgroup 内全部进程（主进程及 detached 孤儿）被内核同时 SIGKILL，实测 `kill -0 <detached_pid>` 返回 `No such process`。
  3. **硬超时预算（RuntimeMaxSec）**：指定 `-p RuntimeMaxSec=2` 启动 `sleep 10`，在 2.18s 时被 systemd 强制以 SIGTERM/SIGKILL 终止，单元状态标记为 `Finished with result: timeout`。
  4. **文件系统隔离（ProtectSystem / ReadOnlyPaths / BindPaths）**：
     - 配置 `-p ProtectSystem=strict -p ReadOnlyPaths=/home` 后，`touch` 写入报错 `Read-only file system`（退出码 1）。
     - 额外配置 `-p BindPaths=/tmp/test_dir` 可精准放行特定工作目录可写。
  5. **网络隔离（PrivateNetwork）**：配置 `-p PrivateNetwork=yes` 后，服务内执行 `ip link` 仅能看到 loopback（`lo`），成功隔离网络。
  6. **启动延迟**：实测单次瞬态单元启动与执行延迟约 **55ms**。
- **受限项与未验证项**：
  1. **内存与进程限制受限（MemoryMax / TasksMax）**：实测配置 `-p MemoryMax=100M -p TasksMax=10` 后，`systemctl --user show` 依然显示 `MemoryMax=infinity`、`TasksMax=28825`。原因在于非特权用户管理器默认未获得系统级 cgroup 控制器委托（`cgroup.subtree_control` 未向非 root 用户开启 memory 委托）。若需内存硬隔离，需宿主管理员在 `/etc/systemd/system/user@.service.d/` 配置 `Delegate=memory`。
  2. **eBPF IP 过滤不可用（IPAddressDeny）**：实测 `-p IPAddressDeny=any` 在非 root 用户单元下为空配置，内核 eBPF 需要 root 权限。
  3. **未验证项**：当用户完全注销会话且未开启 `loginctl enable-linger` 时的后台稳定性尚未长期观测。

### 2.2 候选 B：`bubblewrap`（bwrap）

- **实机可用性验证**：已安装 `/usr/bin/bwrap`（v0.9.0）。
- **实测通过项**：
  1. **非特权用户命名空间（Unprivileged User Namespaces）**：普通用户可直接使用，完全无需 `sudo` 或 suid 特权。
  2. **极速启动延迟**：实测执行 `bwrap ... python3` 启动延迟仅 **15ms**，是三者中性能最优、开销最小的方案。
  3. **精准细粒度文件系统隔离**：
     - 可通过 `--ro-bind / /` 将宿主根文件系统设为只读。
     - 可通过 `--tmpfs ~/.ssh`、`--tmpfs ~/.config` 直接在内存中掩盖敏感凭据目录（实测 `ls ~/.ssh` 输出为空）。
     - 可通过 `--bind <worktree> <worktree>` 仅开放指定任务工作树可写。
     - 可通过 `--tmpfs /tmp` 隔离临时文件。
  4. **原生网络隔离（--unshare-net）**：实测 `--unshare-net` 启动后，容器内 `ip link` 仅包含 `lo`，完全无法访问局域网或外部互联网。
- **受限项与未验证项**：
  1. **自身不管理 cgroup**：bwrap 仅负责 Linux Namespaces（Mount, Net, IPC, PID, UTS, User），本身不提供 cgroup 内存与 CPU 限额。
  2. **孤儿进程清理机制**：虽然支持 `--die-with-parent`，但若内部有多层进程且主进程崩溃，需要配合 Linux PR_SET_CHILD_SUBREAPER（即当前的 `attempt_supervisor`）或 systemd 才能确保 100% 连根拔起。
  3. **未验证项**：若未来模型工具需要执行复杂的系统调试工具（如 gdb/ptrace），与 bwrap 默认 seccomp 规则的兼容性尚未全面测试。

### 2.3 候选 C：`Docker` 容器

- **实机可用性验证**：Docker 29.1.3 已安装且 dockerd 常驻运行，当前用户在 `docker` 组中，可直连 `/var/run/docker.sock`。
- **实测通过项**：
  1. **严苛的资源隔离**：`-m 128m --cpus 1.0` 可被 dockerd 严格下发到内核 cgroup。
  2. **完备的网络隔离**：`--network none` 彻底切断网络。
  3. **镜像级干净环境**：本地已缓存 `python:3.12-slim` 与 `ubuntu:20.04` 镜像。
- **严重缺陷与安全阻断项**：
  1. **特权穿透风险极高（Root Equivalent）**：通过 `/var/run/docker.sock`，任何拥有 Docker 权限的进程均可挂载宿主根目录实现逃逸，本质等同于给予 Agent 宿主 root 权限，严重违背可信自治执行的权限约束原则。
  2. **启动延迟极高（~1.08s）**：实测单次 `docker run --rm` 耗时 1083ms，是 bwrap 的 70 倍、systemd 的 20 倍，高频任务开销过大。
  3. **文件权限错位（UID/GID Mismatch）**：容器内默认以 root 运行，挂载宿主工作树写出的文件所有者为 root，会导致宿主端 Git 及后续进程 Permission Denied。
  4. **环境依赖沉重**：每次模型修改需要的 Python 依赖或 CLI 工具必须预装在 Docker 镜像内，维护成本高。

---

## 3. 三种技术路线横向打分矩阵

| 评估维度 | 权重 | systemd-run --user | bubblewrap (bwrap) | Docker |
| :--- | :---: | :---: | :---: | :---: |
| **凭据与路径隔离度** | 25% | 良好（ProtectSystem/BindPaths） | **优秀**（零特权 tmpfs 遮蔽） | 良好（容器镜像隔离） |
| **全后代终止确定性** | 25% | **优秀**（cgroup v2 强行全杀） | 良好（依赖 subreaper 联动） | **优秀**（容器生命周期） |
| **网络隔离能力** | 15% | 良好（PrivateNetwork 阻断） | **优秀**（--unshare-net 原生） | **优秀**（--network none） |
| **安全性（零特权/防逃逸）** | 15% | 优秀（用户态无 sudo） | **优秀**（纯非特权命名空间） | **差（docker.sock 相当于 root）** |
| **性能与启动开销** | 10% | 良好（~55ms） | **极高（~15ms）** | 差（~1080ms） |
| **内存/CPU 严格预算** | 10% | 需宿主 Delegate 配置 | 需与外部 cgroup 联动 | **原生支持** |
| **综合建议指数** | 100% | **推荐作为外层监管** | **推荐作为执行隔离层** | **强烈不推荐** |

---

## 4. 推荐方案（Recommendation）

基于上述实机测试与安全分析，提出如下架构推荐：

### 核心结论：采用「`systemd-run --user` 瞬态单元」与「`bubblewrap`（bwrap）内部沙箱」正交组合（双层防御架构）

1. **外层边界（生命周期与停止）：`systemd-run --user`**
   - 职责：提供 attempt 专属的瞬态 cgroup、`RuntimeMaxSec` 绝对时间预算、以及 `systemctl --user stop` 的不可逃逸内核级全后代强杀。
   - 解决痛点：彻底消除孤儿进程和超时挂死风险，与现有 `attempt_supervisor` 的 PR_SET_CHILD_SUBREAPER 形成双保险。
2. **内层边界（权限与路径隔离）：`bubblewrap`**
   - 职责：在瞬态单元内部包裹具体的 Runner 进程。通过 `--ro-bind / /`、`--tmpfs ~/.ssh`、`--tmpfs ~/.config` 屏蔽宿主凭据，通过 `--bind <worktree> <worktree>` 仅暴露任务工作树，通过 `--unshare-net` 关死外部网络。
   - 解决痛点：彻底解决宿主敏感文件泄露与网络私自连接风险，且无需任何 root 权限，仅需 15ms 延迟。
3. **轻量降级路径**：
   - 若系统环境不支持多层嵌套，则优先采用 **`attempt_supervisor`（PR_SET_CHILD_SUBREAPER 负责全后代收割与停止证明）+ `bwrap`（负责文件/网络/凭据隔离）**，此方案在当前 Linux 5.15 内核上已完全具备所有前置条件，零外部守护进程依赖。

---

## 5. 选型落地与实测结论

1. **已落地的 M1 适配器架构（`AttemptBoundSandboxAdapter`）**：
   - **宿主前置绑定（`binding.json`）**：在 `agentcore/attempt_supervisor.py` 与 `AttemptBoundSandboxAdapter.create_host_bound` 中，由宿主于执行前以 `0o600`、`O_CREAT | O_EXCL | O_NOFOLLOW` 原子写入 `binding.json`，持久绑定 `job`、`attempt`、`token`、`nonce`、不可变 `instance_id`、`generation`、`boot_id`、`supervisor_pid` 与 `supervisor_start`。严禁任务 `payload` 自行声明上述字段，严禁使用事后停止回执（`stop.json`）开启事前准入门禁。
   - **内核命名空间与文件系统隔离（`bubblewrap`）**：通过 `bwrap --unshare-all --unshare-user --disable-userns --die-with-parent --new-session --clearenv` 建立独立 User/PID/Net/Mount/IPC/UTS 命名空间；以 `--tmpfs` 遮蔽 `/home`、`/run`、`/var`、`/tmp`，彻底隐藏宿主凭据（`~/.ssh`、`~/.config`、`~/.hermes`）与控制套接字（`/var/run/docker.sock`）；将工作树外系统挂载及工作树内 `.git` 与岗位 `never` 路径（通过 `validate_role_path_access` 判定）设为内核只读（`EROFS`）。
   - **五维资源预算硬约束（`ResourceBudgets`）**：
     - 时间预算（`timeout_s`）：由专属沙箱 subreaper 监控超时并连根收割；
     - 进程预算（`max_procs`）：依托 `--unshare-user` 在独立用户命名空间内设置 `RLIMIT_NPROC`，精确限制沙箱内派生进程数而不受宿主用户总进程数干扰；
     - 内存与单文件预算（`max_memory_bytes`、`max_file_bytes`）：通过 `RLIMIT_AS` 与 `RLIMIT_FSIZE` 内核强制限制；
     - 输出与磁盘预算（`max_output_bytes`、`max_disk_bytes`）：流式截断输出并通过 `--size` 限制 `/tmp/scratch` tmpfs 容量，同时对工作树增量字节超限执行自动回滚。
   - **两阶段取消与 Worker 崩溃自愈**：`adapter.cancel()` 先阻断新动作，再通过控制套接字通知沙箱 subreaper 终止并收割全部普通及 `setsid`/`double-fork` 后代；当 Worker 被 `SIGKILL` 强杀时，沙箱 subreaper 立即感知控制套接字 `EOF` 并自动收割全部后代进程。
2. **默认 Fail-Closed 门禁**：
   - `colleague_worker._trusted_containment_available` 仅对通过 `verify_attempt_binding` 核验的 `bwrap_subreaper` 预执行绑定放行；`gateway_delegated`、`host_owned`、无适配器或伪造证明一律拒绝。
