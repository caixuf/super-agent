# agentcore

**一句话定位**：一个与业务无关的长期记忆引擎 —— 存储、双路召回、分桶、冲突判定、
置信度衰减这些机制固定实现；「领域取值 / 代际识别 / 默认身份 / 嵌入后端 / 向量维度」
这些**业务约定由宿主注入**。

`agentcore/` 包内源码禁止出现任何具体企业的业务字面量，这条硬约束由
`tests/test_business_neutrality.py` 自动门禁。

---

## 安装

```bash
# 从源码安装（含测试依赖）
python3 -m pip install -e ".[test]"

# 仅运行不安装也可：仓库根目录直接跑
python3 -m pytest tests/ -q
```

要求 Python >= 3.9。运行时零第三方依赖（标准库 `sqlite3` + 可选 `sqlite-vec` 扩展）。

`agentcore/memory/vendor/vec0.so` 是 sqlite-vec 扩展，**随包分发**，召回时按包相对
路径查找（不依赖当前工作目录）。扩展缺失时向量层自动降级到 Python 逐条计算，
相关测试 skip 而不是 fail。

---

## 最小可用示例（写入 → 召回）

```python
import hashlib, math, re
from pathlib import Path
from agentcore.memory.config import MemoryConfig, configure

def fake_embed(texts, dim=64):
    """确定性离线嵌入：字符 n-gram 哈希投影 + 归一化。生产环境换成真模型。"""
    out = []
    for t in texts:
        v = [0.0] * dim
        s = (t or "").lower()
        grams = list(re.findall(r"[a-z0-9_]+", s))
        for run in re.findall(r"[\u4e00-\u9fff]+", s):
            grams += [run[i] for i in range(len(run))]
            grams += [run[i:i + 2] for i in range(len(run) - 1)]
        for g in grams:
            h = hashlib.sha256(g.encode("utf-8")).digest()
            v[int.from_bytes(h[:8], "big") % dim] += 1.0 if h[8] & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v] if n else v)
    return out

# 1) 注入宿主约定（这里是通用场景：唯一带代际的领域叫 automotive）
configure(MemoryConfig(
    data_dir=Path("./.agentcore/data"),
    embed_dim=64,
    embed_fn=lambda texts: fake_embed(texts, 64),
    era_domains=("automotive",),
    # 代际的「写法」也由宿主约定：这里约定写成 "G1" / "G2.1"（引擎默认认 R 系写法）
    era_pattern=r"\bG(\d+)(?:\.\d+)?\b",
    era_prefix="G",
))

from agentcore.memory import memory_store as ms

# 2) 写入（异步落向量）
ms.write_fact(
    "阈值门限超过 3.0% 时退出自动模式",
    ebm="G1",
    subject="阈值门限",
    tags={"topic": "threshold"},
)

# 3) 断言/退出前汇合后台向量线程（返回仍未完成的线程数）
assert ms.flush_background(timeout=30) == 0

# 4) 召回（代际硬隔离：G1 查询只回 G1 与 _unknown）
result = ms.recall_facts("阈值门限", limit=3, ebm="G1")
for hit in result["results"]:
    print(hit["workspace_id"], hit["statement"], hit.get("_score"))

# 空库/无命中时 result["empty_reason"] 会说明原因（no_match / below_score_floor / ...），
# 而不是返回一个假的成功。
```

要点：

- **写入是异步的**：`write_fact` 提交后后台线程落向量，断言前必须
  `flush_background()`；进程正常退出时 `atexit` 也会自动汇合，脚本/CLI 不会丢索引。
- **全新目录可直接用**：目录创建与建表下沉到连接工厂，无需先手工初始化。
- **代际硬隔离**：`ebm` 归一成桶名写入 `workspace_id`，跨代永不互相覆盖。

---

## 宿主需要注入哪些业务约定

| 约定 | `MemoryConfig` 字段 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 领域取值 | `seed_domains` / `seed_genres` | `general, system, middleware, ai, ops, people` / `experience, reference, preference, procedure` | 仅首次建库种子；之后取值存 SQLite 注册表，新领域自动登记，引擎不枚举 |
| 代际识别 | `era_domains` / `era_pattern` / `era_prefix` / `unknown_bucket` | `()` / `\bR(\d+)(?:\.\d+)?\b` / `R` / `_unknown` | 哪些领域带代际、怎么从文本识别、桶名前缀；空 = 无代际维度 |
| 默认身份 | `default_user` / `default_agent` | `default_user` / `default_agent` | 未显式指定 `user_id` / `agent_id` 时的落库与召回身份，读写两侧共用 |
| 嵌入后端 | `embed_fn` | `None`（内置外呼兜底） | 签名 `(Sequence[str]) -> Sequence[Sequence[float]]`；注入离线/自定义后端，测试必须用确定性实现 |
| 向量维度 | `embed_dim` | `1024` | 必须与嵌入后端输出一致，否则 vec0 建表维度错误、KNN 全空 |
| 领域词表 | `seed_domain_signals` / `seed_source_prefixes` | 通用词表 | 业务领域关键词与来源文件前缀；引擎只提供通用领域打分机制 |
| 数据目录 | `data_dir` | `~/.agentcore/data`（可被 `AGENTCORE_DATA_DIR` 覆盖） | 事实库、注册表、突触账本同目录 |
| 批量灌入 | `bulk_ingest` | `False` | 放宽 fsync，用于可重跑的批处理 |

---

## 目录结构

```
agentcore/
├── __init__.py
└── memory/
    ├── __init__.py
    ├── config.py            # 业务可调项与全局配置注入
    ├── memory_store.py      # 写入 / 双路召回 / 后台汇合
    ├── vec_index.py         # sqlite-vec 可选加速层
    ├── memory_registry.py   # 领域/体裁注册表（数据驱动）
    ├── memory_buckets.py    # 三维分桶兼容层
    ├── memory_scope.py      # 作用域（platform/reference/ops）
    ├── memory_kind.py       # 记忆类别（technical/personal/context）
    ├── domain_infer.py      # 领域推断（词表由宿主注入）
    ├── memory_gate.py       # 入库门禁
    ├── memory_conflict.py   # 冲突判定
    ├── memory_plasticity.py # 突触强化
    ├── memory_ingest.py     # 文档批量灌入
    ├── memory_sync.py       # 同步
    ├── memory_trace.py      # 追踪
    ├── memory_trigger.py    # 自动触发
    └── vendor/vec0.so       # sqlite-vec 扩展（随包分发）
```