# 内存泄漏专项核查报告

结论先行：**没有发现进程级内存泄漏**。发现的是**三处「单调增长且永不回收」的结构性占用**，
其中一处是真正的泄漏性质（常驻副本随库增长而放大），两处已在本轮修掉。

## 一、先排除「进程泄漏」

网关进程（pid 3131951）已连续运行 20 小时、47 线程、57 个会话、1943 条消息。

- RSS 连续采样 20 分钟：523120 → 523296 → 522768 kB，**无上升趋势**；
- `/proc/<pid>/smaps_rollup`：`Anonymous: 511060 kB`，几乎全部是匿名内存，
  无异常映射；
- 20 分钟后复查仍在 522–524 MB 区间。

**判定：网关不是泄漏源。** 宿主机上 RSS 最大的进程是外部 IDE 的 node（2.88 GiB）
与另一个仓的 `local_model_gateway`（1.4 GiB），都不属本仓。

因此「系统内存占用」的真正来源不在进程堆，而在**记忆引擎的存储与它的常驻副本**。

## 二、真正的泄漏：常驻副本随库增长而放大

`agentcore/memory/read_view.py` 用 `source.backup(self.conn, ...)` 把整个源库
拷进 `:memory:`。`agentcore/memory/recall_service.py` 起服务时按
`--view-mib 3072` 给它 3 GiB 预算。

**这不是「拷贝一次就固定」——它把源库此刻的全部内容搬进常驻内存**，
所以源库每长大一分，常驻内存就跟着涨一分。这就是「泄漏」性质的来源：
一个只增不减的库，配一个忠实复制它的常驻副本。

现场实测（`agentcore/memory_footprint.py`，只读）：

```
store      1925.9 MiB
facts      195368, embeddings 191574
vectors    1605.8 MiB (83.4% of store), duplicated=True
dormant    116517 of 191574 embeddings (60.8%) belong to retired facts
view       fits (budget 3072 MiB)
```

**库内 83.4% 的字节是向量，而其中 60.8% 属于永远召不回来的退役事实。**

### 为什么「退役事实的向量」是纯浪费

召回恒过滤 `status = 'active'`（`memory_store.py` 的 KNN 与 LIKE 两条路径都带
`f.status = 'active'`）。而 `drop_fact()` 与 `supersede()` **只改 `status`，
从不释放向量**。本机复现确认：

```
写入后         status=active     embeddings=1  vec_facts=1
drop_fact 后   status=deprecated embeddings=1  vec_facts=1   <- 向量未释放
supersede 后   status=deprecated embeddings=2  vec_facts=2   <- 仍未释放
```

现场这些退役事实来自一次 `book_ingest`（120311 条，全部 `deprecated`）。
它们按设计要保留正文（「当时为什么这么定」要能回答），但**保留正文不等于
必须保留向量** —— 向量只是召回用的索引，退役事实的向量永远不会被检索到。

按 1024 维 float32 = 4096 字节算：退役向量的 BLOB 侧约 451 MiB，
`vec_facts` 索引侧再一份，**合计约 902 MiB**，且全部会被拷进常驻副本。

### 清理方案的安全边界（已量化）

退役事实的 subject 与活跃事实**大量不重叠**，所以绝大多数退役向量
连「可能仍被引用」这一层顾虑都不存在：

```
退役事实总数          : 120311
  subject 仍有活跃版本: 2937  (2.4%)
  subject 已无活跃版本: 117374 (97.6%)

退役事实的向量条数            : 115484
  其中 subject 已无活跃版本   : 112677  -> 合计约 880.3 MiB
  其中 subject 仍有活跃版本   : 2807    -> 合计约 21.9 MiB
```

即：**约 880 MiB 属于「subject 已彻底退役」的向量，清掉它们不触及任何
仍可能活跃的主题**；剩下 21.9 MiB 涉及仍有活跃版本的 subject，保守起见暂不动。
清理因此可以分两级走，不必一刀切。

另需注意：**退役事实的正文全部保留**（`working_fact` 行不动），
「当时为什么这么定」仍然答得出来；被删的只是**召回索引**。

### 三个成因的叠加

| 成因 | 现场量 | 能否被召回用到 |
| --- | --- | --- |
| `fact_embeddings` BLOB | 842.1 MiB | 仅 active 部分 |
| `vec_facts` shadow chunk（**同一批向量的第二份**） | 752.8 MiB | 仅 active 部分 |
| 退役事实的向量（两份合计） | 约 902 MiB | **完全不能** |

即：库里有约 902 MiB 是「永不参与召回的向量，且存了两份」，
再被 `read_view` 原样搬进常驻内存。

## 三、两处会话级缓存的无界增长（已修）

长跑网关里两处按 key 累积、从不回收的容器：

| 位置 | 键 | 后果 |
| --- | --- | --- |
| `plugins/task_progress` 编辑锁表 | `(platform, chat_id, message_id)` | **每条消息留一把锁，永不回收** |
| `plugins/local_model_grounding` `_verdicts` / `_injected` | `session_id` | 每个会话留一条，永不回收 |

已改为按上限逐出，并实测确认上限生效：

```
task_progress 编辑锁表: 20000 次调用后条目数 = 512 (上限 512)
grounding 50000 会话后: _verdicts = 256 (上限 256)
grounding 50000 会话后: _injected = 256 (上限 256)
```

编辑锁的逐出有一条硬约束：**只丢未被持有的锁**。换掉一把正在被等待的锁，
等价于让同一条消息被改两次 —— 这条已写成断言（`test_a_held_edit_lock_is_never_evicted`）。

## 四、嵌入缓存的常驻占用（已修）

`_EMBED_CACHE` 原先存 Python float list。分进程实测（4000 条 × 1024 维）：

```
list    cache RSS =  159.7 MiB
packed  cache RSS =   24.0 MiB     -> 省 135.7 MiB
```

改为 packed float32 字节，取值时才解包。附带把超限时的**整表清空**
改成 FIFO 逐出 —— 整表清空会让缓存周期性归零，紧接着的一批请求全部 miss，
缓存等于没有；逐出只丢最旧几条。

## 五、排除掉的项目

为避免「看着像增长其实有治理」的地方被误判，逐项查过：

| 疑似点 | 判定 |
| --- | --- |
| 变更日志 `memory_view_changes` | **有治理**。`change_journal.prune()` 按 `retain_events` 与 `max_events//4` 双重封顶，并带 `max_events` 容量上限 |
| 突触账本 `memory_usage.db` | 现场仅 279 行、92 KiB，无异常 |
| 回合 spool `memory-spool` | 现场 62 个文件 / 280 KiB，**终态文件不删**（`completed`/`discarded` 永久留在盘上），属磁盘增长不是内存；当前量级可忽略，但同样无上限 |
| SQLite 连接泄漏 | `memory_store` 内每个 `_connect()` 都有对应 `close()`，未发现泄漏 |
| `read_view` 增量应用 | 有 `max_delta_bytes` 预算，且超出即 `ViewInvalid`，不是无界增长 |
| 向量索引 `vec_facts` | 由 `ensure_table`/`upsert` 维护，写入失败会回滚，无孤儿累积（现场孤儿仅 1033 条 / 0.5%） |

## 六、待决（未执行，需授权）

按收益从大到小：

1. **清退役事实的向量** —— 分两级：
   先清「subject 已无活跃版本」的 112677 条（**约 880 MiB 磁盘 + 同量常驻内存**），
   这部分不触及任何仍活跃的主题；涉及仍有活跃版本的 2807 条（21.9 MiB）暂不动。
   只删 `fact_embeddings` / `vec_facts` 的行，**保留 `working_fact` 正文**，
   因此「当时为什么这么定」仍可回答。属破坏性操作，须走
   `agentcore/memory/index_maintenance.py` 的快照式流程并留档。
2. **让 `drop_fact` / `supersede` 顺带释放向量** —— 堵住**未来**同类增长的源头，
   而不只是清理存量。需谨慎：`superseded` 与 `deprecated` 的语义不同
   （前者要能回答历史），但向量对两者都没用。
3. **消除向量双存** —— `fact_embeddings` 与 `vec_facts` 二选一。属 schema 变更，
   `vec_facts` 是 sqlite-vec 虚拟表，不能简单 DROP，需独立设计。
4. **常驻视图预算** —— `--view-mib 3072` 是现场值；清理后须按实际库大小重测。
   `runtime/BASELINE.md` 的纪律要求现场值一律重测，不照搬。

另：**WSL 宿主**层面 swap 已用 5.3 GiB，说明存在换页压力。宿主 31.7 GiB 上
WSL 上限 24 GiB + Windows 自身开销；`.wslconfig` 改动需 `wsl --shutdown`
才生效，会中断所有 WSL 任务 —— 属需用户拍板的宿主变更，本轮未动。
