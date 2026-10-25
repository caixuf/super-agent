# super-agent —— 通用智能体本体

**一句话定位**：把一个「和你一样强的智能体」拆成可 clone、可幂等安装的技术仓。

这里只有**通用机制**：身份纪律骨架、记忆引擎、工具面适配、运行时骨架、平台适配补丁、安装器。
**任何业务内容都不进这个仓** —— 业务字面量由仓级棘轮门禁 `tests/test_repo_neutrality.py` 拦，
业务侧一律以**叠加层**形式提供（业务技能源、业务工具面、业务知识库、业务身份段）。

> 判定标准只有一句：**clone 一个仓，能不能把它原样立起来？**
> 不能立起来的那几层，就是迁移之痛的来源 —— 见 `docs/MIGRATION.md` 与 `docker/` 的干净室验证。

---

## 分层

| 层 | 目录 | 落位 | 是什么 |
| :--- | :--- | :--- | :--- |
| 身份与纪律 | `identity/` | `<HERMES_HOME>/agentcore/identity/` | 通用纪律骨架 `SOUL.generic.md`（业务处用 `{{BUSINESS_OVERLAY}}` 占位）+ 派工角色契约 `agents/*.md` |
| 记忆引擎 | `agentcore/` | pip 包 | 长期记忆：存储 / 双路召回 / 分桶 / 冲突判定 / 置信度衰减；业务约定全部注入式 |
| 工具面 | `toolsets/` | `<HERMES_HOME>/agentcore/toolsets/` | 外部编码执行器适配器（长任务派出去跑，不用主 agent 逐行磨） |
| 运行时 | `runtime/` | `<HERMES_HOME>/agentcore/runtime/` | 常驻服务单元骨架（占位符化，profile 名/服务名都不是写死的）+ `BASELINE.md`（能力基线与无人值守档：通用键名 / 地板值）+ `CONFIG_MAP.md`（段归属、并行会话语义、派工协议陷阱） |
| 文档 | `docs/` | —— | `MIGRATION.md`（八层迁移清单 + 「只在盘上」资产表）、`PLUGINS.md`（插件 / MCP / 工具集注册的边界与验收口径） |
| 平台适配补丁 | `patches/` | `<HERMES_HOME>/agentcore/patches/` | 消息平台：流式卡片 + 纯文字审批（不依赖卡片回调）；幂等重放/校验 |
| 安装器 | `install/install.sh` | —— | 幂等、`--dry-run` 零副作用、不启停服务、不碰凭据、不覆盖现场身份 |
| 干净室验证 | `docker/` | —— | 容器里从一个空 HOME 装一遍，20 项断言 |
| 迁移口径 | `docs/MIGRATION.md` | —— | 「只在盘上」的漏项清单 + 可量化验收 |

---

## 快速开始

```bash
bash install/install.sh --help              # 全部开关
bash install/install.sh --dry-run           # 只打印计划，**不改系统**
bash install/install.sh --profile <name>    # 真装（通用资产；示例技能不会被装成生产技能）
bash install/install.sh --profile <name> --compose-soul <业务叠加层.md>   # 合成生效身份
bash install/install.sh --profile <name> --apply-baseline                # 显式写入**通用能力基线**（默认不动配置；地板只抬不降）
```

安装器**不做**：不启停任何服务（systemd 命令只打印给人执行）、不读写 `.env` / 凭据、
不覆盖现场身份（默认不动 `SOUL.md`）、不改部署源（只读）。

---

## 干净室部署验证（Docker）

```bash
bash docker/verify.sh              # 默认 HEAD：导出干净检出 → 建镜像 → 跑 20 项断言
bash docker/verify.sh <commit>     # 验证某个已发布提交（发布前跑这个）
```

数字为准，覆盖七组：

| # | 断言 |
| :--- | :--- |
| ① | 仓自检：业务无关门禁 + 引擎单测全绿 |
| ② | 包安装：**离开仓目录**仍可 import（不是靠 cwd 假绿），随包 `.so` 在包里 |
| ③ | `--dry-run` **一个文件都不建**（名副其实） |
| ④ | 真实安装：资产字节一致、**绝不覆盖现场身份**、**示例技能不得装成生产技能**、没有 Hermes 时补丁步骤优雅跳过而非报错 |
| ⑤ | 幂等：重跑识别出 N 处「已一致」、文件集合不变 |
| ⑥ | 合成生效身份：骨架 + 业务叠加层，`{{BUSINESS_OVERLAY}}` 必须被替换干净 |
| ⑦ | 引擎端到端：写入 → 汇合 → 召回（离线确定性嵌入，不依赖网络/模型） |

**最近一次验证报告**：[`docs/reports/2026-10-25_clean_room_deploy_report.md`](docs/reports/2026-10-25_clean_room_deploy_report.md)
（同一目录另有 `.html` 单文件版可分享）—— 26 项断言全通过，含当轮修复的缺陷记录与「明确不覆盖」清单。

**它已经抓到过什么**：`install/install.sh` ⑩ 段 heredoc 里的 `$HERMES_HOME` 没做兜底，
在 `set -u` 下**空机器上会以 `unbound variable` 非 0 退出**（活全干完了却报失败）。
本机因为恰好导出了那个变量，这个 bug 一直没暴露 —— 这类「只在我机器上能跑」的缺陷，
就是本目录存在的理由。

---

## 特性验证（宿主侧）

干净室回答「装得上、装完能用」；**特性**是否成立要另跑一套（需要本机有 Hermes / Node）：

```bash
bash checks/feature_check.sh              # 8 组特性断言（不碰生产 profile：一律用临时 home）
bash checks/identity_injection_check.sh   # 证明「身份文件真的进了发给模型的 prompt」
```

| 组 | 测的是什么 |
| :--- | :--- |
| F1 | 身份骨架的八节纪律项齐备 |
| F2 | 合成语义：注入点**唯一**、骨架原样在前、体积等式成立、标记无残留 |
| F3 | 派工契约：角色文件齐备，且都含「回执字段」与「派工卡」 |
| F4 | 示例技能规范（name 与目录一致、description 可被触发匹配） |
| F5 | **业务叠加钩子**：默认不装业务技能；显式开钩子才装得进来 |
| F6 | 记忆闭环：写入 → 汇合 → 召回 → 改写命中 → 代际隔离 → 空结果语义 → 换目录可用 |
| F7 | 平台适配补丁：`--check` 幂等在位 + `--config` 读到**生效值**（不是写了配置就完事） |
| F8 | 外部执行器：真跑一个小任务，**回读产物**（回执说成功不算证据） |
| F9 | 运行时骨架不写死部署身份 |
| F10 | 明确打印**未覆盖**清单（别把绿当全能） |

`identity_injection_check.sh` 的判定链值得一提：它把隔离 profile 的模型端点指向一个**死端口**，
调用必然失败 → Hermes 在 API 出错时会**无条件**写请求转储 → 在转储里找骨架独有句。
于是「身份是否注入」变成可机械判定的断言，**不需要可用凭据**；同时它备份生产 profile 的
`config.yaml`，跑完 diff 必须无变化，证明隔离没串到生产。

本轮实测报告：**`docs/reports/2026-10-26_identity_and_feature_verification_report.md`**
（特性矩阵 31 PASS / 注入判定 14 PASS / 干净室 28 PASS，并列出本轮修掉的 7 个真缺陷 ——
包括「骨架根本没有叠加层注入点」这种**检查通过但机制是坏的**假绿）。

---

## 业务叠加层（本仓不生成任何业务内容）

| 业务资产 | 由谁提供 |
| :--- | :--- |
| 业务身份段（业务纪律、领域知识） | 业务仓 → `--compose-soul <叠加层.md>` |
| 业务技能源 | 业务仓 → `INSTALL_BUSINESS_SKILLS=1 BUSINESS_SKILLS_SRC=<dir>` |
| 业务工具面 / 业务知识库 / 业务 provider | 业务仓按 `runtime/README.md` 与 `identity/README.md` 的占位符契约补齐 |

默认**全部为关**：不装业务技能、不生成业务内容、不动现场身份。

---

# 附：记忆引擎（`agentcore` 包）

**一句话定位**：一个与业务无关的长期记忆引擎 —— 存储、双路召回、分桶、冲突判定、
置信度衰减这些机制固定实现；「领域取值 / 代际识别 / 默认身份 / 嵌入后端 / 向量维度」
这些**业务约定由宿主注入**。

`agentcore/` 包内源码禁止出现任何具体企业的业务字面量（由 `tests/test_repo_neutrality.py`
与 `tests/test_business_neutrality.py` 双重门禁）。

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

# 1) 注入宿主约定（这里是通用场景：唯一带代际的领域叫 automotive，代际写成 "G1"/"G2.1"）
configure(MemoryConfig(
    data_dir=Path("./.agentcore/data"),
    embed_dim=64,
    embed_fn=lambda texts: fake_embed(texts, 64),
    era_domains=("automotive",),
    era_pattern=r"\bG(\d+)(?:\.\d+)?\b",   # 引擎默认就认 G 系写法，这里显式写一遍做示范
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

`docker/engine_smoke.py` 就是这个示例的可运行版本（干净室验证第 ⑦ 步用的就是它）。

## 宿主需要注入哪些业务约定

| 约定 | `MemoryConfig` 字段 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 领域取值 | `seed_domains` / `seed_genres` | `general, system, middleware, ai, ops, people` / `experience, reference, preference, procedure` | 仅首次建库种子；之后取值存 SQLite 注册表，新领域自动登记，引擎不枚举 |
| 代际识别 | `era_domains` / `era_pattern` / `era_prefix` / `unknown_bucket` | `()` / `\bG(\d+)(?:\.\d+)?\b` / `G` / `_unknown` | 哪些领域带代际、怎么从文本识别、桶名前缀；空 = 无代际维度。**默认只给一个中性示例写法，真代际命名由宿主注入** |
| 默认身份 | `default_user` / `default_agent` | `default_user` / `default_agent` | 未显式指定 `user_id` / `agent_id` 时的落库与召回身份，读写两侧共用 |
| 嵌入后端 | `embed_fn` | `None`（内置外呼兜底） | 签名 `(Sequence[str]) -> Sequence[Sequence[float]]`；注入离线/自定义后端，测试必须用确定性实现 |
| 向量维度 | `embed_dim` | `1024` | 必须与嵌入后端输出一致，否则 vec0 建表维度错误、KNN 全空 |
| 领域词表 | `seed_domain_signals` / `seed_source_prefixes` | 通用词表 | 业务领域关键词与来源文件前缀；引擎只提供通用领域打分机制 |
| 数据目录 | `data_dir` | `~/.agentcore/data`（可被 `AGENTCORE_DATA_DIR` 覆盖） | 事实库、注册表、突触账本同目录 |
| 批量灌入 | `bulk_ingest` | `False` | 放宽 fsync，用于可重跑的批处理 |

## 目录结构

```
super-agent/
├── identity/          # 身份与纪律（通用骨架 + 派工契约）
├── agentcore/
│   └── memory/        # 记忆引擎
│       ├── config.py            # 业务可调项与全局配置注入
│       ├── memory_store.py      # 写入 / 双路召回 / 后台汇合
│       ├── vec_index.py         # sqlite-vec 可选加速层
│       ├── memory_registry.py   # 领域/体裁注册表（数据驱动）
│       ├── memory_buckets.py    # 三维分桶兼容层
│       ├── memory_scope.py      # 作用域
│       ├── memory_kind.py       # 记忆类别
│       ├── domain_infer.py      # 领域推断（词表由宿主注入）
│       ├── memory_gate.py       # 入库门禁
│       ├── memory_conflict.py   # 冲突判定
│       ├── memory_plasticity.py # 突触强化
│       ├── memory_ingest.py     # 文档批量灌入
│       ├── memory_sync.py       # 同步
│       ├── memory_trace.py      # 追踪
│       ├── memory_trigger.py    # 自动触发
│       └── vendor/vec0.so       # sqlite-vec 扩展（随包分发）
├── toolsets/          # 工具面（外部编码执行器适配）
├── runtime/           # 运行时骨架（服务单元）
├── patches/           # 平台适配补丁（消息平台）
├── install/install.sh # 幂等安装器
├── docker/            # 干净室部署验证
├── docs/MIGRATION.md  # 迁移口径与漏项清单
└── tests/             # 门禁 + 引擎单测
```