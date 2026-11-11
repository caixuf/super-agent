# 03. 研发专家协同与状态图引擎 (Multi-Agent, StateGraph & Supervisor)

> **文档导航**：[Wiki 首页 (Home)](Home.md) │ [01. 单根受管部署与极速上手](01-Quickstart-and-Install.md) │ [02. 业务知识叠加与服务编排](02-Business-Overlay-Guide.md) │ [03. 研发专家协同与状态图引擎](03-Multi-Agent-and-StateGraph.md) │ [04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md) │ [05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)

---

## 1. 8 大官方研发角色契约与路径所有权矩阵

### 1.1 故障场景：角色越界与代码践踏
在多智能体自主研发环境中，若缺乏明确的权限边界，测试专家为了让测试通过可能会直接修改门禁断言，或者前端角色误删核心协议代码，导致系统协同崩溃。

### 1.2 真实代码机制：`DEV_ROLES` 与 `RolePathOwnership`
`agentcore/colleague_dev.py` 统一定义了 8 大官方研发专家岗位契约（外层网关负责对外接待分诊，不属于 `DEV_ROLES` 内部成员）：

| 角色标识 (`role`) | 岗位名称 (`display_name`) | 核心职责与操作边界 (`duty` & `boundaries`) |
| :--- | :--- | :--- |
| `architect` | 需求边界与系统架构师 | 梳理需求契约与输入输出规格；负责 `SPEC.md` 与 `architecture/**`，禁止触碰 `case_set/**` |
| `cases` | 核心研发与用例开发专家 | 专职编写确定性验证用例与业务代码；管辖 `case_set/**` 与 `lib/**`，禁止触碰 `.git/**` |
| `reviewer` | 代码审查与红队质检员 | 专职执行安全审计与红队挑刺；管辖 `red_team_review.yaml`，禁止修改 `src/**` 或 `agentcore/**` |
| `test` | 自动化测试与仿真验证专家 | 批量执行测试套件并统计覆盖率；管辖 `test_run.log`，禁止直接篡改业务源码或 `.git/**` |
| `triage` | 缺陷定性与根因诊断专家 | 抓取日志并对账状态轨迹；负责输出 `ROOT_CAUSE.md`，禁止直接合入未验证的代码修复 |
| `backend` | 服务端与底层系统工程师 | 负责底座基础设施与通信协议；管辖 `runtime/**`，变更记录追加至 `CHANGELOG.md` |
| `frontend` | 表现层与交互排版工程师 | 专职设计流式交互卡片与图文交付；管辖 `static/**` 与 `templates/**` |
| `ops` | 平台稳定性与运维专家 | 负责集群配置对账、巡检诊断与应急恢复；管辖 `docker/**` 与 `configs/**` |

权限受控通过不可变数据结构 `RolePathOwnership` 进行硬拦截：
```python
@dataclass(frozen=True)
class RolePathOwnership:
    role: str
    owns: Tuple[str, ...] = ()       # 完全控制：允许创建、修改、重命名与删除
    appends: Tuple[str, ...] = ()    # 仅允许追加日志或事实，严禁删除已有内容
    never: Tuple[str, ...] = ()      # 严禁触碰：任何写操作立即抛出 PermissionError
    artifacts: Tuple[str, ...] = ()  # 允许产出的交付物模式
    enforce_write: str = HARD        # "hard" 硬强制写拦截
    enforce_read: str = SOFT         # "soft" 软提示，不阻断只读探针
```

### 1.3 代价与边界
`enforce_write="hard"` 确保非责任角色绝不污染关键源码，但若任务拆解不当将导致合理修改被拦截；此时须按职责流转给对应责任角色。

---

## 2. 模型路由与架构决策记录 (ADR-S1 ~ ADR-S6)

`agentcore/model_routing.py` 严格定义了模型档位分配与降级铁律：

| 决策编号 | 架构决策名称与代码真相源 | 具体防护机制 | 代价与系统边界 |
| :--- | :--- | :--- | :--- |
| **`ADR-S1`** | **唯一路由源** (`model_routing`) | 所有 Runner 必须通过本模块统一解析模型，严禁私自硬编码 | 失去局部自由覆盖灵活性 |
| **`ADR-S2`** | **显式降级标记** (`degraded=True`) | 发生算力降级时必须记录 `degradation_reason` 并透传至终验卡片 | 调用方需处理降级元数据 |
| **`ADR-S3`** | **记忆提炼防降级保护** | `compression`、`memory` 角色**严禁降级至 `flash`**，否则抛出 `ValueError` | **宁可报错中断也绝不静默污染长期记忆** |
| **`ADR-S4`** | **单向无环包边界** | `agentcore` 与 `toolsets.pi_executor` 严格保持单向依赖，禁止反向导入 | 依赖解耦需要定义共享接口 |
| **`ADR-S5`** | **Pro 白名单与配额守卫** | 仅 `PRO_ALLOWED_ROLES` 允许使用最高算力，单任务默认上限 2 次 (`DEFAULT_MAX_PRO_CALLS_PER_TASK = 2`) | 限制深度思考模型的调用次数 |
| **`ADR-S6`** | **终态卡片互斥守卫** | `durable-dispatch` 与 `task-progress` 产出终态卡片时排他互斥 | 杜绝单任务多卡冲突 |

---

## 3. 确定性状态图引擎 (`agentcore/state_graph.py`)

### 3.1 故障场景：死循环空转与临时分支残留
智能体在自主循环探索（如 `Execute <-> Evaluate`）中易陷入局部死循环白白消耗 Token；或在终态退出时残留未提交代码，造成成果丢弃。

### 3.2 真实代码机制与使用模式
`StateGraph` 采用构建器模式，支持编译期校验与执行期断点接管：
```python
builder = StateGraph()
builder.add_node("plan", plan_func)
builder.add_node("exec", exec_func)
builder.set_entry_point("plan")
builder.add_edge("plan", "exec")
builder.add_edge("exec", END)

# 编译参数支持持久化与安全检查
graph = builder.compile(
    checkpointer=SqliteCheckpointer(db_path),  # SQLite WAL 模式断点恢复
    stagnation_limit=5,                        # 连续 5 步无状态跃迁熔断
    worktree_harvest_check=True                # 终态检查 Git Worktree 是否干净
)
```

1. **三类检查点持久化实现**：
   - `SqliteCheckpointer`：开启 WAL 模式，断电重启后自动根据 `task_id` 跳过已完成 step 恢复；
   - `MemoryCheckpointer`：内存测试检查点；
   - `JsonCheckpointer`：本地轻量 JSON 检查点。
2. **停滞守卫 (`stagnation_limit`)**：
   在节点跳变时计算状态的规范化 JSON MD5 哈希。若连续 `stagnation_limit` 步（默认 5 步）状态指纹无进展，立即抛出 `StagnationDetectedError` 熔断退出。
3. **终态 Worktree 物理收割检查 (`worktree_harvest_check=True`)**：
   流转至终点前，执行 `git status --porcelain` 检查工作区。若发现存在未提交或未跟踪的脏改动，抛出 `WorktreeStrandedError` 阻断退出。

### 3.3 代价与边界
状态哈希要求所有业务 State 值必须支持规范化 JSON 序列化；终态 Worktree 检查增加了子进程执行 Git 命令的文件 I/O 开销。

---

## 4. 进程监管器防逃逸与 PID 复用防御 (`attempt_supervisor.py`)

### 4.1 故障场景：孤儿子进程逃逸与 PID 复用误杀
长任务子进程可能通过 `double fork()` 脱离父进程组变成孤儿进程；而在超时强杀时，若子进程退出后其 PID 被操作系统迅速分配给系统服务，盲目 `kill` 会误杀关键进程。

### 4.2 真实代码机制
`agentcore/attempt_supervisor.py` 作为独立的单线程管理主程序运行：
1. **Linux Subreaper 接管**：调用 `ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)`（`PR_SET_CHILD_SUBREAPER`），强制让所有脱离的孤儿子孙进程重新挂载到 supervisor 下；
2. **控制通道握手协议**：接管 control socket (`sys.argv[1]`)，就绪后向父进程发送 `b"ready"`，接收 `b"go"` 确认后才拉起目标子进程；
3. **PID 复用指纹比对**：组合读取 `/proc/{pid}/stat` 第 20 字段（`starttime` 时钟节拍）与 `/proc/sys/kernel/random/boot_id`。指纹不匹配时坚决拒杀；
4. **不可变证据链**：若子进程因提权导致 `PermissionError` 无法清理，将其记录至 `unquiesced_pids`，生成不可变 `evidence.json` 并以退出码 125 退出。

### 4.3 代价与边界
仅支持 Linux 平台；非 root 权限无法杀死其他 UID 提权进程；强杀超时等待（`REAPING_DEADLINE_SECONDS = 5.0`）会增加同步退出耗时。

---

## 5. 延伸参考
- 交互卡片与终态交付：[04. IM 交互卡片与交付机制](04-Feishu-Cards-and-Delivery.md)
- 运维健康体检与故障对账：[05. 运维体检与排障对账](05-Operations-and-Troubleshooting.md)
- 架构设计原则与 ADR 汇总：[`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)
