# super-agent —— 通用智能体本体

## 持久任务、研发同事与执行层

- **持久任务池**：`agentcore.pool` 负责持久任务、依赖、租约与结果 outbox；`agentcore.worker` 通过有界 `AgentScheduler` 调用 stdlib 或真实 `flowcoro_py` 后端。支持取消、超时、输出预算和旧 attempt 隔离。
- **只读分析运行器**：`agentcore.analysis_runner` 执行只读分析任务，产出 `citation_traceability_only` 结构化引用事实。
- **研发同事运行器**：`agentcore.dev_runner` 在独立的 Git Worktree 中执行编码与测试任务，配备 `read, write, edit, bash` 工具，由跨进程 `fcntl.flock` 文件锁保证单一写者，产出 `dev_traceability_v1` 证据链。
- **研发同事模型**：`agentcore.colleague_dev` 定义了 8 大官方研发岗位契约（需求架构师、用例开发大师、红队评审员、仿真测试专家、归因专家、后端开发、前端卡片交互、运维巡检），支持编排闭环与平台 SOP 流水线原生桥接。

使用与恢复边界见 [docs/POOL_RUNTIME.md](docs/POOL_RUNTIME.md) 与 [docs/DISPATCH_ANALYSIS.md](docs/DISPATCH_ANALYSIS.md)。


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
| 身份与纪律 | `identity/` | `<HERMES_HOME>/agentcore/identity/` | 通用纪律骨架 `SOUL.generic.md`（业务处用 `{{BUSINESS_OVERLAY}}` 占位）+ 派工角色契约 `agents/*.md` + 同事模型规范 `COLLEAGUE_MODEL.md` |
| 研发同事与执行 | `agentcore/` | pip 包 | 8 大官方研发角色契约（`colleague_dev.py`）+ 具备全套编码测试工具（`read, write, edit, bash`）与独立 Git Worktree 文件锁隔离运行器（`dev_runner.py`）+ 任务持久池与调度器（`pool.py`, `worker.py`） |
| 记忆引擎 | `agentcore/memory/` | pip 包 | 长期记忆：存储 / 双路召回 / 分桶 / 冲突判定 / 置信度衰减 / 突触账本；业务约定全部注入式 |
| 任务活体状态 | `agentcore/liveness.py` + `plugins/task_status/` | pip 包 + 插件 | 长任务账本：心跳 / 阶段 / 进度 / pid 证据 / 终态写一次；`stalled` 与 `lost` 分开，询问时返回 elapsed、heartbeat age、bounded tail，不靠轮询猜测 |
| 工具面 | `toolsets/` | `<HERMES_HOME>/agentcore/toolsets/` | 外部编码执行器适配器（`pi_executor`：长任务派出去独立跑，不用主 agent 逐行磨） |
| 运行时 | `runtime/` | `<HERMES_HOME>/agentcore/runtime/` | 常驻服务单元骨架（占位符化，profile 名/服务名非写死）+ `BASELINE.md`（通用能力基线）+ `CONFIG_MAP.md`（段归属与并发语义）+ `autotask/` 定时作业规范 |
| 技能库 | `skills/` | `<HERMES_HOME>/skills/` | 通用与示例技能库（`illustrated-doc-rendering`, `im-card-markdown`, `research-before-build`, `skill-distillation`），业务技能通过挂钩环境变量注入 |
| 运维与审计工具 | `tools/` | —— | 部署复验 `redeploy_check.py`、T6 缺口审计 `t6_gap_audit.py`、AI 定时作业编译器 `tools/autotask/`、文档架构浏览器等 |
| 文档 | `docs/` | —— | `MIGRATION.md`（八层迁移清单 + 资产表）、`PLUGINS.md`（插件/MCP/工具集注册）、`POOL_RUNTIME.md`、`DISPATCH_ANALYSIS.md`、`REDEPLOY.md` |
| 平台适配补丁 | `patches/` | `<HERMES_HOME>/agentcore/patches/` | 消息平台适配：Card 2.4 流式卡片 + 纯文字审批；幂等重放与内容探针校验 |
| 插件集 | `plugins/` | `<HERMES_HOME>/plugins/<name>` + profile `config.yaml` 的 `plugins.enabled` | `longtask_guard/`（前台长任务自动转后台 + 心跳/账本登记）、`task_status/`（长任务活体快照：state/elapsed/heartbeat age/tail）、`durable_dispatch/`（持久任务提交与状态查询）、`colleague_team/` 与 `colleague_catalog/`（研发团队管理与目录呈现）、`markdown_delivery/`（大文档直发与租户级共享）、`write_guard/`（写保护门禁）等 13 款通用插件 |
| 安装器 | `install/install.sh` | —— | 幂等、`--dry-run` 零副作用、不启停服务、不碰凭据、不覆盖现场身份 |
| 干净室验证 | `docker/` | —— | 容器里从一个空 HOME 装一遍，20 项断言 |
| 迁移口径 | `docs/MIGRATION.md` | —— | 「只在盘上」的漏项清单 + 可量化验收 |

---

## 快速开始

```bash
bash verify.sh                 # ← 先跑这个：静态 + 单测 + 安装 dry-run（约 30 秒，零依赖）
bash verify.sh --with-docker   # 再加干净室部署验证（建镜像，几分钟）
bash verify.sh --with-checks   # 再加宿主特性矩阵（需 Hermes 树 / Node；缺依赖的组降级 WARN）

bash install/install.sh --help              # 全部开关
bash install/install.sh --dry-run           # 只打印计划，**不改系统**
bash install/install.sh --profile <name>    # 真装（通用资产；示例技能不会被装成生产技能）
bash install/install.sh --profile <name> --compose-soul <业务叠加层.md>   # 合成生效身份
bash install/install.sh --profile <name> --apply-baseline                # 显式写入**通用能力基线**（默认不动配置；地板只抬不降）
```

`verify.sh` 的退出码 = 失败项数（0 = 全过），缺依赖导致的跳过会在末尾明说 ——
「环境缺失」不许伪装成「通过」。它会自己挑一个**真的带 pytest 的解释器**（PATH 上的
`python3` 常常先命中没有 pytest 的沙箱 venv，那会让单测块假红，人会去怀疑代码而不是环境）。

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
| ④ | 真实安装：资产字节一致、**绝不覆盖现场身份**、**`examples/` 只当范本（真通用技能会被正常安装）**、没有 Hermes 时补丁步骤优雅跳过而非报错 |
| ⑤ | 幂等：重跑识别出 N 处「已一致」、文件集合不变 |
| ⑥ | 合成生效身份：骨架 + 业务叠加层，`{{BUSINESS_OVERLAY}}` 必须被替换干净 |
| ⑦ | 引擎端到端：写入 → 汇合 → 召回（离线确定性嵌入，不依赖网络/模型） |

换机器上线只认一条路径：[`docs/REDEPLOY.md`](docs/REDEPLOY.md)（`tools/redeploy_check.py` 一条命令复验干净 clone，
`tools/t6_gap_audit.py` 逐条报 T6 缺口；插件「装了」不等于「工具面里看得见」，清单与重启步骤都写在里面）。

**最近一次验证报告**：[`docs/reports/2026-10-26_release_freeze_report.md`](docs/reports/2026-10-26_release_freeze_report.md)
（候选提交 `6333c92`，干净室**退出码 0**；同一目录另有 `.html` 单文件版可分享。）
> 更早的两份报告（`2026-10-25_clean_room_deploy_report.md`、`2026-10-26_identity_and_feature_verification_report.md`）
> 验证对象停在 `74b13e0` / `2f23720`，**已过期**：其后 8~9 个提交引入的两处红没跟着复验。
> 引用「通过率」时只认最新这份 —— 报告停在旧提交上，就是仓库自述与事实矛盾。

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

特性矩阵与身份注入的**上一轮**实测报告：`docs/reports/2026-10-26_identity_and_feature_verification_report.md`
（特性矩阵 31 PASS / 注入判定 14 PASS，并列出当轮修掉的 7 个真缺陷）。
> [说明] 该报告的验证对象是 `2f23720`，**本轮未复跑**（F7/F8 需真实 Hermes 树、Node 与可解析凭据）。
> 最新一次干净室 + 本地结果只认 [`2026-10-26_release_freeze_report.md`](docs/reports/2026-10-26_release_freeze_report.md)；
> 「特性」侧的未复跑状态记在该报告 §4 缺口。

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

## 存储占用体检（只读）

记忆库的**存储与常驻内存**占用由独立命令测量，不靠人猜：

```bash
python3 -m agentcore.memory_footprint --database <记忆库路径> --view-budget-mib <常驻视图预算>
```

输出只有条数与字节数（无事实正文、无向量值）：库体积、向量占比、
**向量是否被存了两份**（`fact_embeddings` BLOB + `vec_facts` 索引）、
以及**多少向量属于退役事实**（召回恒过滤 `status='active'`，退役事实的向量
仍占着存储，并被 `read_view` 原样拷进常驻副本）。

配套巡检作业见 `runtime/autotask/jobs/memory-footprint-guard.json`——
它只**报告**，不删库、不重建索引、不改配置。

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
├── identity/                  # 身份与纪律
│   ├── SOUL.generic.md        # 通用纪律骨架（占位符化）
│   ├── COLLEAGUE_MODEL.md     # 同事制角色分层与边界模型
│   └── agents/                # 派工角色叶子契约 (reader/author/runner/triage/closure 等)
├── agentcore/                 # 核心引擎包
│   ├── memory/                # 长期记忆引擎（存储/双路召回/vec0/冲突判定/突触账本）
│   ├── pool.py                # 持久任务队列、POSIX 任务锁与隔离
│   ├── worker.py              # 有界任务 Worker 与执行器适配
│   ├── scheduler.py           # 调度器门控与超时控制
│   ├── colleague_dev.py       # 8 大官方研发同事角色契约与编排器
│   ├── dev_runner.py          # 具备编码/测试工具的独立 Git Worktree 隔离运行器与文件锁
│   ├── analysis_runner.py     # 只读分析运行器（输出引用可追溯证据）
│   ├── colleague_catalog.py   # 规范化同事名录与描述提取
│   ├── colleague_conversation.py # 同事会话生命周期与路由
│   ├── colleague_evidence.py  # 研发证据链校验 (dev_traceability_v1)
│   ├── markdown_delivery.py   # 大文档递送与共享权限
│   └── card_emit.py           # 卡片流式状态发射与存储
├── plugins/                   # 通用插件集
│   ├── colleague_team/        # 研发同事团队管理（招聘/授权/停用）
│   ├── colleague_catalog/     # 同事名录与岗位描述查询
│   ├── durable_dispatch/      # 持久任务提交、状态查询与取消
│   ├── longtask_guard/        # 前台长任务自动转后台保护
│   ├── markdown_delivery/     # 大文档云端直发与租户级共享
│   ├── write_guard/           # 文件写操作守卫与安全拦截
│   ├── capability_alert_outbox/ # 能力异常与漂移告警发件箱
│   └── illustrated_delivery/  # 图文排版与渲染交付
├── skills/                    # 通用技能库
│   ├── illustrated-doc-rendering/ # 图文排版与渲染
│   ├── im-card-markdown/      # 消息卡片 Markdown 转换
│   ├── research-before-build/ # 建前预研规范
│   └── skill-distillation/    # 技能提炼与抽取
├── tools/                     # 运维、测试与审计工具
│   ├── redeploy_check.py      # 一键重建复验
│   ├── t6_gap_audit.py        # T6 迁移缺口逐条审计
│   ├── install_skills.py      # 技能安装器
│   └── autotask/              # 定时 AI 作业规范编译器 (taskfile.py/cron.py)
├── toolsets/                  # 外部工具适配面
│   └── pi_executor/           # 外部代码隔离执行器适配
├── runtime/                   # 运行时服务单元骨架与基线
│   ├── agentcore-worker@.service # Worker 服务模板
│   ├── BASELINE.md            # 能力基线与通用键地板值
│   ├── CONFIG_MAP.md          # 配置段归属与并行语义
│   └── autotask/jobs/         # 生产定时巡检作业模板
├── patches/                   # 宿主平台适配补丁（Card 2.4 / 流式卡片等）
├── install/                   # 安装器与插件启用脚本 (install.sh, enable_plugins.py)
├── docker/                    # 干净室部署验证 (verify.sh, Dockerfile)
├── docs/                      # 架构、迁移与规范文档
└── tests/                     # 仓级门禁、单测与集成测试
```