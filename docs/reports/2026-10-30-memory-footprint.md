# 系统内存占用诊断与优化

本轮目标：降低**系统 RAM 常驻占用**。先测量，再改代码；每项改动都单独验证。

## 一、现场测量（本机 WSL，24 GiB 上限 / 宿主 31.7 GiB）

进程级（`ps` + `/proc/<pid>/status`）：

| 进程 | RSS | Swap | 归属 |
| --- | --- | --- | --- |
| node（Antigravity IDE server） | 2.88 GiB | 534 MiB | 外部 IDE |
| local_model_gateway | 1.4 GiB | 1.0 GiB | <platform-root> |
| omp | 549 MiB | 42 MiB | <platform-root> |
| hermes 网关（pid 3131951） | 523 MiB | 61 MiB | 本仓宿主 |
| bge-m3-sidecar | 463 MiB | 1.15 GiB | 嵌入服务 |

`free -m`：总量 24033，已用 8531，free 1470，**swap 已用 5317**。

网关 RSS 连续采样 20 分钟（523120 → 522768 kB）**不增长**，20 小时 uptime 稳定。
**结论：网关不是泄漏源；本仓自身的 RAM 大头不在进程，而在记忆引擎的存储与副本。**

## 二、记忆引擎的真实占用（`agentcore/memory_footprint.py`）

现场库 `~/.hy_memory/data/coding_memory_cv.db`：

```
store      1925.9 MiB (bucket <=2048)
facts      195368, embeddings 191574
vectors    1605.8 MiB (83.4% of store), duplicated=True
dormant    116517 of 191574 embeddings (60.8%) belong to retired facts
view       fits (budget 3072 MiB)
  fact_embeddings                     842.1 MiB
  vec_facts_vector_chunks00           752.8 MiB
  working_fact                         82.5 MiB
```

三个已量化的成因：

1. **向量存了两份**：`fact_embeddings` BLOB 842 MiB + `vec_facts` shadow chunk 753 MiB，
   合计占库 83.4%。
2. **60.8% 的向量属于退役事实**：召回恒过滤 `status = 'active'`，
   `deprecated` 的 120311 条事实各带一条向量，召回永远不返回，却照占存储。
3. **常驻副本**：`read_view` 用 `source.backup()` 把整个库拷进 `:memory:`，
   现场 `--view-mib 3072` 下常驻约 1.9 GiB —— 上面两条会原样放大到内存里。

## 三、本轮改动

### 1. 嵌入缓存改存 packed bytes（`agentcore/memory/memory_store.py`）

缓存值从 Python float list 改为 packed float32 字节，取值时才解包。
**实测（分进程，4000 条 × 1024 维）：159.7 MiB → 24.0 MiB，省 135.7 MiB。**

同时把超限时的**整表清空**改为 FIFO 逐出：整表清空会让缓存周期性归零，
紧接着的一批请求全部 miss，缓存等于没有；逐出只丢最旧几条。

### 2. 插件内的会话级缓存加上限

- `plugins/task_progress`：编辑锁表 key 含 `message_id`，长跑网关为每条消息留一把锁，
  从不回收。改为按上限逐出，且**只丢未被持有的锁**（换掉正在等锁的协程等价于失去互斥）。
- `plugins/local_model_grounding`：`_verdicts` / `_injected` 按会话累积，改为按上限丢最旧。

### 3. 新增只读占用报告（`agentcore/memory_footprint.py`）

之前 `tools/memory_budget_report.py` 只管 `MEMORY.md` 字符数，**看不见存储与常驻副本**。
新工具只测量、只分类，不删不重建不 vacuum；输出只有条数与字节数，无事实正文、无向量值。

配套巡检作业 `runtime/autotask/jobs/memory-footprint-guard.json`（每日一次，
超预算或向量重复存放时告警，但**不自行**删库/重建索引/改配置）。

## 四、验证

- 记忆相关回归 275 通过 / 2 跳过（可选依赖）。
- `task_progress` / `local_model_grounding` / `localvec` 相关 350 通过 / 3 跳过。
- 全量：**1648 通过 / 30 跳过 / 1 失败**。
- 唯一失败项 `test_repo_neutrality` 与本轮改动**无关**：
  `docs/reports/2026-10-30-stability-handover.md` 是上一轮遗留的**未跟踪**文件，
  含业务字面量，被仓级中立性棘轮拦下。本轮未新增任何字面量违规。
- 新增测试：`tests/test_memory_embed_cache_footprint.py`（3）、
  `tests/test_memory_footprint.py`（8）、
  以及 `test_task_progress_plugin.py` / `test_local_model_grounding.py` 中的上限用例。

## 五、未做与待决

以下三项**都需要改线上记忆库或改宿主配置**，超出本轮「改本仓代码」的授权，未执行：

1. **清退役事实的向量**（可回收约 975 MiB 存储 + 同量常驻副本）。
   涉及删 `fact_embeddings` / `vec_facts` 中的行 —— 属破坏性操作，
   须先走 `agentcore/memory/index_maintenance.py` 的快照式流程并留档。
2. **消除向量双存**（`fact_embeddings` 与 `vec_facts` 二选一）—— 属 schema 变更，
   需独立设计；`vec_facts` 是 sqlite-vec 虚拟表，不能简单 DROP。
3. **常驻视图预算**：`--view-mib 3072` 是现场值，需按清理后的实际库大小重测；
   `runtime/BASELINE.md` 的纪律要求现场值一律重测，不照搬。

另：**WSL 宿主层面**的 swap 已用 5.3 GiB。宿主 31.7 GiB 上 WSL 上限 24 GiB +
Windows 自身开销，swap 使用本身说明存在换页压力。`.wslconfig` 需 `wsl --shutdown`
才生效，会中断所有 WSL 任务 —— 属需用户拍板的宿主变更，本轮未动。
