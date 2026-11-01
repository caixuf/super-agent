# 全自动记忆优化 — 方案与 main 执行契约

日期：2026-11-01，Asia/Shanghai。
状态：P1 隔离实现与父级验收完成，代码及经验归档推送；P2/P3 未执行，不是已上线。

## 1. 目标（用户契约）

用户明确要求全自动记忆系统优化，并指定父级规划、main 模型执行。
常规链路不能依赖用户逐条审批：完成回合 → 自动提取 → 自动证据/持久性/敏感信息审查 → 去重/冲突裁决 → 幂等入库与索引 → 跨会话召回验证 → 可观测质量评估。
不把已观察到的早停修复、HTTP 200 或入库数量增加称为智能提升。

## 2. 背景、输入与已核对边界

工作仓：`/home/caixuf/code/super-agent`，初始 HEAD `89b95f7`。
先读技能：`/home/caixuf/.claude/skills/agent-context-boundary-principle/SKILL.md`。
先读交接：`docs/handoff/2026-11-01-memory-health.md`、`docs/handoff/2026-11-01-conversation-followup.md`、`docs/reports/2026-10-29_flash-memory-candidates.md`。
复用入口：`agentcore/memory/{turn_spool,runtime_bridge,event_worker,runtime_service,memory_trigger,memory_store,memory_conflict,memory_admission,memory_trace,memory_plasticity}.py`。
已核对：`propose_facts` 仅产候选；`runtime_service` 仍调用另一条可能写入的 `run` 路径；冲突判断有 fallback coexist。需 main 在动手前重新核对，冲突以当前源码为准并记录。
`agentcore/learning/CandidateStore` 面向外部不可信资料且禁止自动审批，不通过伪造人工 reviewer 绕开它；区分已完成用户回合与外部网页两类信任源。
原始用户改动 15 个文件，哈希基线：`/tmp/super-agent-memory-autonomy-original-20261101.json`。这些文件不许改、不许提交、不许 reset/stash。
154 completed / 4 discarded 是既有历史诊断快照，不是本轮实时计数。生产积压不读取正文、不消费、不修改。

## 3. 执行阶段与状态

### P1 本次执行：可运行的自动准入闭环（main 负责）

- 沿既有终态 spool / canonical queue / lease worker 接线，不引入第二个回合调度器或第二个事实库。
- 提取器与自动审查职责分离：原文锚定只是来源证据，不是事实真值；模型自报 high 不能自行放行。自动审查有受信宿主注入边界和版本化确定性约束，不把对话当指令执行。
- 区分用户长期偏好/明确决策、客观技术结论、暂态命令与外部引文：明确长期偏好可自动准入；技术结论无独立执行证据时不得升级为“实测已验证”。
- 自动输出 admit / reject / defer（命名可沿既有语义），不引入日常人工必经状态。不确定证据隔离；后端故障有界退避重试；预算耗尽如实失败/隔离，不无限重试、不 fail open。
- 所有候选有来源/身份/内容摘要/策略版本/审查原因/结果审计。敏感凭据不进入正式库、普通日志或报告；坏样本隔离后不能挡住后面的正常样本。
- 同身份同事件重放/崩溃恢复不重复入库；profile/user/agent 与代际隔离；去重及冲突失败不能默认并存；无法可靠裁决时保留旧事实、隔离新候选。
- 仅在正式事实与所需索引工作完成后确认成功。自动召回验证须对正确身份/正确事实检查；失败不能伪装 no_match 或已完成。
- 隔离的离线评测入口输出精度/召回、错误准入、重复率、隔离数、失败数和耗时/模型调用数（成本无法实测时明确缺失），不得用写入量代替质量。

### P2 后续生产激活（不属于本次对子 agent 授权）

父级审查并独立复跑后，确定宿主实际 bootstrap、模型/嵌入后端与 worker 生命周期。精确文件备份、hash drift 校验、受监督的灰度启用、失败自动停写/回退；生产 154 条积压走与新增回合相同版本流水线，限批限速，有单条审计，不直接灌库。未完成此阶段不能称“线上全自动记忆已接通”。

### P3 持续自优化

复用现有 trace/plasticity/health 能力；候选策略先离线与影子评测，固定金标安全门不得被优化器修改。只有在安全指标不退化、有效召回改善且资源预算满足时才能自动晋升；保留版本与回退。无真实模型金标评测时不宣称质量提升。P1 须提供可复现评测基础，而非伪造 P2/P3 已完成。

## 4. 输出与写入边界

main 在自己的 fork 直接改代码，回传文件清单及可应用补丁/commit。允许写 `agentcore/memory/**`、必要的 MemoryConfig 注入项、`tests/test_memory_autonomous_pipeline.py` 及受影响的 memory 测试、`tools/memory_autonomy_eval.py`、此 handoff 和必要的 memory 文档。改动最小、复用优先，不引入依赖。若需要碰原始 15 个用户改动文件或扩张外部知识学习审批，先报告具体阻断，不自行扩大范围。

父级此刻本地任务：冻结契约、记录原始哈希与现有测试基线；main 执行后父级独立审查与复跑，父级不并行改同一实现。只派一个 main 维持队列/审查/写入共享契约，不再拆出另一个实现者。

## 5. 可机械验证的验收

```sh
python3 -m pytest tests/test_memory_autonomous_pipeline.py -q
python3 -m pytest tests/test_memory_auto_loop.py tests/test_memory_event_worker.py tests/test_memory_extraction_outcomes.py tests/test_memory_terminal_spool.py tests/test_memory_runtime_bridge.py tests/test_memory_flash_candidates.py tests/test_memory_health.py tests/test_learning_candidates.py tests/test_learning_review_cli.py tests/test_learning_promotion.py tests/test_repo_neutrality.py -q
python3 tools/memory_autonomy_eval.py --offline
git diff --check
```

金标至少包括：明确长期偏好自动落库并重启后可召回；本轮命令不记；问句/否定/假设/助手自述不记；未完成/自动定时回合不记；凭据拒绝；同一事件重放仅一次；跨身份/代际不泄漏；审查超时/非法 JSON/索引失败不标成功；冲突判断失败保留旧事实；坏样本不阻塞后续有效样本；无人工调用也能完成一轮自动链路。评测样本与阈值写死版本，不为过测降低门槛。

新 test/CLI 名称可按实际设计调整但必须同步本文件命令。回归缺失文件不许静默跳过；若环境阻断须给精确证据。可追加全仓隔离回归，禁止依赖测试访问生产。

## 6. 证据回传

main 回传：与输入冲突的前提、修改文件清单、实际命令、通过/失败/跳过数与耗时、离线评测输出、自动化边界与未验证项、补丁/commit 位置、此 handoff 更新。不能只回“完成”或自报 ok。父级独立验收后才决定是否可发布。

## 7. 禁止事项

禁止生产配置加载、启动生产 worker、重启网关、外发真实用户对话、处理生产积压、读取/打印密钥、变更模型配置、自动降低准入门禁、伪造人工审查者、强推或全仓提交、accept/rewrite 用户原始改动。只允许隔离临时库与合成样本。真实模型测试必须独立声明且仅合成内容，不能用模拟调用证明真实质量。

## 进度记录

- 规划：已完成，main 未回执前不宣称实现/部署完成。

- 派工：main / Lovelace，agent `01a19565-0c3e-7972-8515-49a7e9ba28e3`，负责 P1；父级负责最终复核。
- 派工后独立检查：原始 15 个文件 SHA-256 全部保持一致。
- 父级独立基线：第 5 节既有 11 组回归命令实际执行 **184 passed in 5.14s**，日志 `/tmp/super-agent-memory-autonomy-baseline-20261101.log`；不是新自动链路的验收结果。

### Main 执行进度（2026-11-01）

- 已完成源码复核：确认自动路径原先仍直连 `memory_trigger.run`；事件幂等键缺 `agent_id`；冲突 LLM 不可用/异常默认 `coexist`；后台索引汇合不能证明单条事实索引已写入并可按正确身份召回。
- 已落代码（尚未完成验收）：新增 `agentcore/memory/memory_autonomy.py`，沿既有 canonical queue、lease worker、extractor、conflict、fact store 接入自动准入编排与脱敏审计；修改 `agentcore/memory/runtime_service.py` 使自动 worker 改走该编排；修改 `agentcore/memory/runtime_bridge.py` 将 `agent_id` 纳入事件幂等身份；修改 `agentcore/memory/memory_conflict.py`，裁决不可用时返回 defer 而非 coexist；修改 `agentcore/memory/memory_store.py`，增加单事实同步索引写入/校验入口。
- 当前进行中：补 `tests/test_memory_autonomous_pipeline.py`、`tools/memory_autonomy_eval.py`，并修正受幂等键版本变化影响的既有测试断言；随后运行第 5 节全部验收并按实际结果回填。
- 已运行检查：上述 5 个 Python 文件均通过 `python3 -m py_compile`；新链路测试与离线评测尚未运行，不宣称完成。父级既有 11 组回归基线为 **184 passed in 5.14s**，日志 `/tmp/super-agent-memory-autonomy-baseline-20261101.log`。
- 当前阻断：无环境硬阻断；剩余工作是新增金标测试/评测、执行全量受影响回归、修复实际失败并完成证据回填。未触及生产、密钥、真实积压或原始 15 个用户改动文件。

### Main 最终执行回执（2026-11-01，P1 代码冻结）

状态：P1 隔离实现与验收完成；**未部署生产、未启动生产 worker、未读取或处理 154/4 生产积压、未加载生产密钥/模型配置**。P2 生产激活和 P3 自动策略晋升仍未执行，不能宣称线上全自动记忆已接通。

#### 与规划前提的源码冲突（已按源码修复）

- 事件幂等键原先未包含 `agent_id`，已升级为 `turn_v2` 并纳入 agent 身份。
- 自动 worker 原经 `runtime_service.run_and_flush()` 调 `memory_trigger.run` 直写；现自动路径改走 `AutonomousMemoryPipeline`。
- 冲突 reviewer 不可用/异常原 fallback `coexist`；自动路径现改为 defer，保留旧事实并隔离候选。手动调用默认语义仍由 `conservative=False` 保持。
- 后台 `flush_background()` 不能证明单条事实已索引；自动路径新增同步单事实索引与真实召回校验。
- 自动候选原在索引/召回成功前可能 active，且 supersede 可能提前生效；现采用 candidate→同步索引→事务内冲突/激活/真实召回，失败整体 rollback。

#### 修改文件（本轮 P1；保留现场，不提交 git）

- `agentcore/memory/memory_autonomy.py`（新增）：版本化自动审查、冻结提取、脱敏审计、canonical queue runner、admit/reject/defer、有界保守冲突、候选激活与精确召回验证。
- `agentcore/memory/memory_store.py`：严格 user/agent/profile 过滤、legacy-shared 显式模式、候选激活事务、快照漂移检查、单事实同步索引、冲突预算。
- `agentcore/memory/memory_conflict.py`：reviewer 失败 defer；自动保守模式与候选/软时间预算；不再默认 coexist。
- `agentcore/memory/memory_admission.py`：不要求引号的多形态 synthetic secret 检测（API key/token/password/密码/Bearer 等）。
- `agentcore/memory/memory_trigger.py`：保留完整 `user_quote`；混合问句中的独立明确决定可进入审查；纯问句仍拒绝；禁止裸子串去限定语。
- `agentcore/memory/memory_kind.py`：补“回复我/回答我”等偏好分类。
- `agentcore/memory/runtime_bridge.py`：事件身份含 agent；prefetch 透传 agent/profile。
- `agentcore/memory/runtime_service.py`：自动 worker 接入新 pipeline；worker observation 不输出对话正文。
- `agentcore/memory/turn_spool.py`：坏 JSON、外身份、非法记录隔离，不阻塞后续正常 spool。
- `agentcore/memory/progressive.py`：L1/L2/L3 透传 agent/profile 并用于取数/召回。
- `agentcore/colleague_memory_cli.py`：team CLI 显式 legacy-shared 模式；手工团队事实共享，自动 completed-turn 事实仅 owner agent/profile 可见。
- `plugins/memory_bridge/__init__.py`：宿主工具入口透传可信 user/agent/profile。
- `tests/test_memory_autonomous_pipeline.py`（新增，37 tests）：P1 金标。
- `tests/test_memory_terminal_spool.py`：坏 spool 隔离契约。
- `tools/memory_autonomy_eval.py`（新增）：离线版本化评测，固定 synthetic conflict reviewer；报告明确非真实模型质量。
- 注：`tests/test_memory_autonomy_review_guards.py` 由父级独占编写，main 未修改。

#### 精确验收命令与实际结果

```text
python3 -m pytest tests/test_memory_autonomy_review_guards.py tests/test_memory_autonomous_pipeline.py -q
59 passed in 11.19s
```

```text
python3 -m pytest tests/test_memory_curated_sync.py tests/test_memory_document_watcher.py tests/test_memory_read_view.py tests/test_colleague_shared_memory_binding.py tests/test_memory_extraction_outcomes.py -q
95 passed, 1 skipped in 19.44s
SKIP: tests/test_colleague_shared_memory_binding.py:77 — missing host dependency `agent.memory_manager`
```

```text
python3 -m pytest tests/test_memory_auto_loop.py tests/test_memory_event_worker.py tests/test_memory_extraction_outcomes.py tests/test_memory_terminal_spool.py tests/test_memory_runtime_bridge.py tests/test_memory_flash_candidates.py tests/test_memory_health.py tests/test_learning_candidates.py tests/test_learning_review_cli.py tests/test_learning_promotion.py tests/test_repo_neutrality.py -q
184 passed in 9.10s
```

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET -u FEISHU_TEST_CHAT_ID python3 -m pytest tests/test_memory*.py tests/test_learning*.py tests/test_colleague_shared_memory_binding.py -q
519 passed, 1 skipped in 50.95s
SKIP: missing host dependency `agent.memory_manager`
```

父级独立稳定扩大回归：519 passed, 1 skipped in 60.94s，日志 `/tmp/super-agent-memory-autonomy-final-expanded-20261101.log`。

```text
python3 tools/memory_autonomy_eval.py --offline
exit 0
cases=10 tp=3 tn=7 fp=0 fn=0
precision=1.0 recall=1.0
wrong_admission_rate=0.0 duplicate_rate=0.0
quarantined=3 failures=0
model_calls=0 conflict_review_calls=1
conflict_reviewer=synthetic_deterministic_fixture
elapsed≈1.956s
production_data_accessed=false
```

评测边界：这些指标仅为固定合成 fixture + 确定性 embedding + synthetic conflict reviewer 的结果，**不是真实模型抽取/冲突理解质量，也不是线上收益**。cost 不可测量，报告中明确 unavailable。

`python3 -m py_compile` 覆盖所有改动 Python 文件通过；`git diff --check` 通过。原始 15 个用户改动文件 SHA-256 再次全部匹配 `/tmp/super-agent-memory-autonomy-original-20261101.json`。

#### 实现安全边界与已验证证据

- 自动审查确定性地覆盖问句、否定/拒绝、不确定、临时命令、外部引文、credential；模型 metadata/subject/ebm/tags/confidence 不信任，由锚定语句确定性派生。
- 混合问句中的独立明确决定不丢；仅孤立 `user_quote` 不足时，审查使用完整用户句上下文。
- 自动候选索引失败保持 `candidate`，不可召回；事件保持 retryable，后续可恢复。
- 激活、supersede 与真实严格召回验证同一 SQLite 事务；召回失败 rollback，新候选不 active，旧事实保持 active。
- 候选裁决前后比较同 user/agent/profile/代际 active ID 快照；漂移回滚重试，拒绝旧裁决覆盖新事实。
- 冲突最多 16 个语义候选；候选窗口最多 256 active；10s 为**调度软预算**，单次 provider 调用仍受现有 30s 上界，故不宣称严格 10s 硬截止。超限一律 defer。
- duplicate 进入旧 fact 自身 subject/statement 的索引与身份召回校验，校验失败 retryable，不新增 active。
- 冻结提取按 event key+policy 持久化并绑定 event content hash 与 profile/user/agent identity digest；不可覆盖，凭据候选不落正文。
- 审计保留首次 decision/reason 与最终 final decision，防 defer 后重试覆写历史。
- 同 user 异 agent/profile 的自动事实在 store、prefetch、L1/L2/L3 均隔离；team legacy shared 路径只共享无 `source_trust=completed_user_turn` 的既有手工团队事实。
- 坏 spool 记录隔离后，后续正常记录继续 enqueue。

#### 未验证边界

- 未接真实宿主 bootstrap、真实模型/embedding provider、生产路径或 worker lifecycle；没有真实模型抽取质量、真实冲突裁决质量或跨会话线上召回证据。
- 全仓父级运行中，唯一已知历史全仓失败为 external eval 读取宿主 `MEMORY.md` 污染；父级已用 HEAD 隔离副本与空 `SUPER_AGENT_MEMORY_FILE` 证明该失败为既有环境基线，不属本次实现。最终合成环境全仓结果以父级日志 `/tmp/super-agent-memory-autonomy-final-all-tests-20261101.log` 为准。
- `agent.memory_manager` 未安装，相关 1 项宿主集成测试 skip；未补宿主依赖。
- 10s 冲突预算为软预算；不存在严格端到端墙钟硬截止保证。
- 无 P2 灰度、无生产回退演练、无 P3 策略晋级/金标质量证明。

#### 交付方式

代码已冻结，保留工作区现场，**未提交 git**。父级最终审查后可自行选择归档/提交；不得包含原始 15 个用户改动。

### 父级独立验收与经验归档（2026-11-01）

- 用户追加授权：修复经验可以提交并 push。新增 `docs/reports/2026-11-01-autonomous-memory-lessons.md`，记录上下文锚定、原子激活/回读回滚、冲突窗口/快照、团队共享与私有事实、冻结审计及测试注入点等可复用经验；不把真实业务知识或凭据写入报告。
- main 实现与父级独立 guard 测试职责分离；父级自有 `tests/test_memory_autonomy_review_guards.py`。最新组合专项 **59 passed in 12.50s**；扩大记忆/学习/共享回归 **519 passed, 1 skipped in 60.94s**，日志分别为 `/tmp/super-agent-memory-autonomy-final-targeted-20261101.log`、`/tmp/super-agent-memory-autonomy-final-expanded-20261101.log`。
- 固定合成离线评测 exit 0：10 项，tp=3/tn=7/fp=0/fn=0，precision/recall=1.0，duplicate=0，quarantined=3，failures=0，真实模型调用0，synthetic conflict review 1 次，耗时2.113s。不代表真实模型质量。
- 第一次全仓因实现迭代及宿主知识污染有失败，不算验收通过。显式提供空合成 `SUPER_AGENT_MEMORY_FILE` 后，HEAD 只读导出与当前树 `tests/evals` 分别 **5 passed in 0.31s / 5 passed in 0.24s**；未放宽金标，未改实体库。
- 合成环境第二次全仓 **2082 passed, 1 failed, 46 skipped in 277.33s**，唯一失败为新 `test_low_jaccard_same_topic_reversal_reaches_judge` 未进入 mock 裁决，专项单独/受影响组合此前通过；main 正补完整 outcome 诊断与稳定性复核，未把该次全仓称为成功。缺失宿主、原生绑定、可选文档依赖与真实消息凭据导致的 skip 如实保留。
- 经验文档追加后，中立性门禁 **17 passed in 0.80s**；`git diff --check` 通过；原始15文件哈希仍全部匹配。
- 提交采用精确文件清单，不使用 `git add .`；不纳入原始15文件；不触及生产配置、worker、网关或历史积压。代码归档不等于部署授权或线上成功。

### main 唯一残余失败调查与再次冻结（2026-11-01）

- 调查对象仅 `test_low_jaccard_same_topic_reversal_reaches_judge`。原全仓失败只证明 `outcome.ok=False/written=0` 且未调用裁决；原 trace 没有完整 outcome，无法确定提前退出 reason。**未证实 SQLite/index race 或模块污染，不能宣称已修复生产竞态。**
- 本次只修改 `tests/test_memory_autonomous_pipeline.py`：失败断言附完整 outcome；保留 `calls` 与旧 fact_id 必须进入候选集的断言，新增必须返回 `synthetic_defer` 的 reason 断言；手工旧事实 setup 后显式 `assert ms.flush_background(timeout=30) == 0`，隔离异步 seed 索引与冲突裁决测试职责。无生产实现改动，无重试吞失败或门槛放松。
- 加屏障前独立进程单项重复 20/20 通过（每次 pytest 0.21–0.39s），日志 `/tmp/super-agent-memory-autonomy-main-single-repeat.log`；同进程直接调用该真实测试函数、每轮独立临时目录/配置/MonkeyPatch，200/200 通过，59.31s，日志 `/tmp/super-agent-memory-autonomy-main-inprocess-stress.log`。均未复现原偶发前置失败。
- 加屏障前前序组合命令 `python3 -m pytest tests/test_localvec_budget.py tests/test_memory_admission.py tests/test_memory_auto_loop.py tests/test_memory_autonomous_pipeline.py -q` 重复5次：每次63 passed，分别6.84s/5.66s/8.01s/9.87s/10.57s；日志 `/tmp/super-agent-memory-autonomy-main-order-repeat.log`。
- **屏障落盘后**实际验收：`python3 -m pytest tests/test_localvec_budget.py tests/test_memory_admission.py tests/test_memory_auto_loop.py tests/test_memory_autonomous_pipeline.py tests/test_memory_autonomy_review_guards.py -q` → **85 passed in 9.48s**；日志 `/tmp/super-agent-memory-autonomy-main-barrier-final.log`。`python3 -m py_compile tests/test_memory_autonomous_pipeline.py` 与 `git diff --check` 通过。
- 父级带完整 outcome 的全仓 `-q -x` 正在 `/tmp/super-agent-memory-autonomy-release-check-20261101.log` 运行；按父级通知已越过60%及故障点未失败。本轮 main 未重复全仓，不预报全仓通过。该运行开始时尚无 setup 屏障，最终屏障由上面的85项组合覆盖。
- 调查结论：原故障为尚未稳定复现、根因未确认的偶发提前退出；补齐诊断与明确测试准备屏障，不把猜测写成根因。代码再次冻结，后续只由父级依据新 trace 决定是否恢复实现。
- 原始15文件 SHA256 复核一致；父级 guards 未写入；未碰 git/远端、未 commit/push、未操作生产/凭据/历史积压。父级负责最终全仓判定及精确 feature branch 归档。

### 父级最终验收与推送决议（2026-11-01）

- 完整前序、合成知识源、禁用真实消息凭据的全仓复跑最终 **2083 passed, 46 skipped in 296.95s**，退出0；日志 `/tmp/super-agent-memory-autonomy-release-check-20261101.log`。命令：`env -u FEISHU_APP_ID -u FEISHU_APP_SECRET -u FEISHU_TEST_CHAT_ID SUPER_AGENT_MEMORY_FILE=<empty-synthetic-file> python3 -m pytest tests -q -x`。46项因宿主、原生绑定、可选依赖或真实凭据条件缺失而跳过，不计通过。
- 上述全仓进程收集时尚无最后的旧事实 setup 汇合屏障。屏障落盘后的父级独立复跑：前序自动记忆/准入、localvec、两组专项与中立性门禁 **102 passed in 10.75s**；日志 `/tmp/super-agent-memory-autonomy-push-check-20261101.log`。生产实现从全仓开始到最终复跑未变。
- 最终离线评测 exit0，10项 precision/recall=1.0、错误准入0、重复0、隔离3、失败0，真实模型调用0、synthetic conflict review1次，耗时0.916s；日志 `/tmp/super-agent-memory-autonomy-push-eval-20261101.log`。仅合成确定性边界验证，不宣称真实模型质量提升。
- 偶发前置失败未能复现，不能称根因已确认。保留此前失败记录、完整 outcome 诊断及准备屏障，后续若再现以明确 reason 追查；本次不伪造 race 根因。
- 推送前 fetch 发现远端 `main` 比本地基线新增 `0cd78cf`、`e7dd078` 两条并行提交，涉及原始保护文件；不 pull/rebase/stash/reset、不覆盖远端。使用独立分支 `feat/autonomous-memory-p1-20261101` 保存本轮18文件，代码仍基于 `89b95f7`，尚未合并远端新增改动；合并时需在最新主线再次集成验证。
- 精确提交范围为本轮11个引擎/CLI修改、新编排、bridge、3个测试文件、eval入口及handoff/经验文档，共18文件；原始15文件SHA-256仍与开工基线相同，不纳入提交。生产仍未启用，历史积压未消费。
