# 发版核对与剩余门禁

> **历史记录说明（2026-11-04）**：本文记录 2026-11-01~11-03 的验证过程与结果。
> 其中 HFC / 外部卡片 sidecar 相关段落、测试名与构建记录属于当时的证据，
> 该整合已从仓库删除，不再代表当前架构。当前源码能力面见
> [`RELEASE_CAPABILITIES.md`](RELEASE_CAPABILITIES.md)；本文的通过 / 跳过数字
> 也只覆盖各自记录时的提交，不能外推到当前 HEAD。

更新日期：2026-11-04（Asia/Shanghai）。
远端基线：`e0a953a`（`origin/main` 与本地 HEAD 一致，`5f5fd1a` 是其祖先）；以下修复已全部推送，但尚未发布。
结论：**当前不具备正式发版证据**。源码测试、候选包安装成功与实际前台、
Worker、记忆、真实渠道可用是不同层次，不能互相替代。

现场只读快照见 `reports/2026-11-03_runtime-topology-audit.md`：有效 Gateway
home 与同名 profile 目录不同，现场尚无新前台边界；现有只读队列绑定一致，
记忆仍为固定单主体。该快照没有实施切换或读取任务/记忆正文。

当前功能输入已包含记忆身份路由、受管 provider 接线、同事绑定读取修复、研发
验收命令授权、有界输出/清理，以及同事 Gateway 派工与完成回执接线。

**本机完整 `verify.sh` 已退出码 0**（2026-11-04 11:25，工作树含同日修复，HEAD `e0a953a`）：
单测块 **2754 passed / 63 skipped / 709.91 秒**，防退化靶场另 **5 passed**，
13 个 shell 脚本语法检查与安装 dry-run 通过，⑥.1 三项延迟门禁全部通过
（DNS 23.2ms、本地 I/O 0.2ms、平台 OpenAPI 133.4ms），⑦ 模型上下文端点
返回 1000000。⑥ 真实通道仍因未设置 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`/
`FEISHU_TEST_CHAT_ID` 按 `verify.sh` 规则记跳过，**不能算通过**。

该轮之前的两轮为红，根因均已在同一天定位并修复，不属代码回归：
首轮 ⑥.1 DNS 解析 1080.4ms 超 300ms 预算 —— 本机 `/etc/resolv.conf` 首选
`223.5.5.5` 对 UDP 无响应，每次解析都要等超时才落到备用解析器；
次轮 ⑥.1 平台 OpenAPI 报 `Connection refused` —— 本机代理端口瞬断，
随后 5 分钟内 133 次连续采样无一次失败。为此给该检查加了**仅针对连接类瞬断**的
一次重试，延迟预算与超时判定不变（`tests/test_preflight.py` 覆盖：拒连后连上=通过、
超时不重试=失败、持续拒连=失败、超预算仍失败）。另修复两处测试辅助函数
`_alive()` 只捕获 `FileNotFoundError` 的问题：进程若在 `open()` 与 `read()` 之间被回收
会抛 `ProcessLookupError`，导致 `tests/test_cancel_request_truth.py` 等用例假红。

断网容器 2474 passed / 138 skipped、候选导出专项 230 passed / 5 skipped 以及更早的
2551 passed / 61 skipped 均属此前功能输入，只作历史基线，不得当作当前源码通过。
完整结果与验证边界见下文“研发验收输出与清理边界”。
目前仍未正式发布，也没有修改生产配置。

后续增量：研发 CLI 新增管理员显式指定的生成／验收适配器入口，修复前
16 条反例均失败；生成、验收、停止及相邻恢复专项为 154 passed / 2 skipped。
该入口尚不代表部署了完整研发 runtime；上文历史全量及候选摘要属于前一功能输入，
不能直接覆盖此增量，须在本轮冻结后重验。

## 已验证的修复

| 范围 | 问题与当前行为 | 证据 |
| --- | --- | --- |
| 原生渲染组件 | 私有推理不再变为可展开内容；公开阶段单独展示 | `tests/test_cardkit2_renderer.py` |
| 异步更新 | drain 超时保留在途句柄；失败显式返回，不能以超时冒充更新完成 | 同上，包含单在途、取消和失败反例 |
| 源码包 | 安装输入完整；干净树才可正式构建；候选明确不可发布；归档可复现且不覆盖已有文件 | `tests/test_package_superagent.py` |
| 导出版本校验 | 不依赖源目录的 `.git`；正常构建元数据不触发误报，源码/模式漂移仍拒绝 | 同上 |
| 受管安装 | 先构建临时目录；同名同内容复用，不删除/重建当前版本；失败不留半成品版本 | `tests/test_superagent_installer.py` |
| 激活与回滚 | 校验内容和新版本文件模式；损坏版本拒绝切换；doctor 检查链接存在且属于当前版本 | 同上；旧受管描述符仅有内容摘要时保留对应内容校验，不冒充模式校验 |
| 取消投影 | 调度工具只记录取消请求，非终态主账不能使活体账本提前终结；完成/失败竞争保留真实结果 | `tests/test_durable_dispatch.py` |
| 本地 HFC 构建 | 已安装发行元数据及修补标记与新增部署记录相符；隔离重跑 8 条投递契约通过 | `tests/test_deployed_hfc_build.py`、`tests/test_hfc_delivery_contract.py`；不是实际 IM/客户端验收 |
| 前台权限子项 | profile 显式启用后，固定白名单覆盖直接/inline 工具、bridge/连接器和 IM 命令；helper/策略故障关闭；并发 profile 和缓存反例通过 | `tests/test_frontdesk_boundary.py`，真实宿主/完整固定候选源码 + 临时 HOME；未生产启用，P1 整体未完成 |
| 候选权限补丁接线 | `candidate-hermes` 保留权限补丁；缺少必需补丁工具拒绝继续，不静默构建缺门禁候选 | 同上及 `tests/test_superagent_installer.py`；普通安装不改现场宿主 |
| 前台回合交接 | 本轮提交回执后宿主生成确认并结束回合，不再请求模型轮询；失败/未知不冒充接单；最后预算回合不误报未完成 | `tests/test_frontdesk_boundary.py` 的真实 AIAgent + Pool 隔离回合，及 `tests/test_frontdesk_turn_release.py`；未实际 IM 验收 |
| 只读隔离纵向链路 | 真实只读 Worker 子进程运行时处理新聊天；同键幂等、跨身份拒绝、重开队列后原路由投递及已确认结果不重复 | 当前宿主与完整固定候选源码均通过；模型和 IM transport 为替身，不是实际服务重启/模型质量/消息平台验收 |
| 独立进程恢复 | 提交进程退出后独立只读 Worker 执行；新 Worker 不重跑完成任务，新交付进程原路由确认，后续交付进程不重复 | `tests/test_dispatch_process_restart.py`；恢复/Worker 相邻专项 29 passed / 1 skipped。仅正常进程退出，不是生产服务重启、在途强杀或停止证明 |
| 记忆测试启动竞争 | socket 路径存在不代表监听就绪；测试改为真实健康握手，并覆盖延迟监听 | `tests/test_memory_recall_service.py`，专项 22 passed，过期排队两种场景重复 10 轮通过；未改生产服务语义 |
| 前台自动复盘 | 即使允许记忆读取，前台也不能通过自动/显式/延迟恢复入口启动记忆技能复盘；损坏策略或 helper 故障同样不启动 | `tests/test_frontdesk_boundary.py`；当前宿主与固定候选均先复现原行为，修复后边界/交接/安装专项 106 passed，且已纳入下述最终全量；外部 provider 捕获不在此项冒充多用户准入 |
| 本地 attempt 停止 | 取消只记请求；本地 runner 用 Linux subreaper 收养、杀死并回收脱离进程组/清空环境的后代，私有停止回执绑定 attempt 后才可终结/恢复 | `tests/test_cancel_request_truth.py`、Pool/Worker/watchdog 专项；含 Worker/监管器强杀与损坏回执反例。不是恶意代码沙箱，不解除研发执行门禁；见 `ATTEMPT_STOP_CONTRACT.md` |
| 启动时前台可用 | 进程启动/ready 等待不持有任务数据库写事务；等待期间前台能查询和派单，取消/暂停使该次 runner 不获启动许可 | 先复现两条阻塞失败，再加入真实监管器受控延迟回归；取消/人审/Worker/恢复/交付相邻专项共 162 passed / 2 skipped |
| 人审与主账一致 | 主账拒绝尚未停止的 attempt 恢复时，卡片不能记为已批准；主账拒绝操作时不消费待处理人审记录 | `tests/test_prefer_c1_hitl.py` 两条反例先失败，修复后纳入上述相邻专项 |
| 监管丢失投影 | 监管器被杀且缺少停止回执时保留绑定身份，前台显示停止尚未确认，不冒充健康运行 | 真实 SIGKILL 反例先复现 `running` 误报，再修复投影；状态/取消/人审相邻专项 108 passed |
| 私有解释器保留 | Worker/记忆服务默认使用调用它们的 Python；显式虚拟环境路径不再解析成缺依赖的基础解释器，并检查可执行权限 | 首轮容器暴露旧 `/usr/bin/python3` 默认错误；真实 venv 回归及相邻专项 39 passed。只生成临时服务文件，未激活生产服务 |
| 容器验证输入 | 无效 ref、非 commit 或导出失败立即退出；构建固定 commit，不混入工作区；检查容器断网 | `tests/test_docker_verify.py`，6 passed；预取安装器真实依赖并用离线 Chromium 验证私有运行时，不用跳过依赖来制造安装通过 |
| 进度卡创建交接 | 后台初始快照读取期间若前台卡片刚好创建完成，仍在锁内加入该任务，不静默丢弃 | 受控线程屏障先复现任务列表为空，修复后进度插件完整模块 53 passed；崩溃恢复编辑意图仍保留未知状态，不盲目重发 |
| 个人记忆身份路由 | 显式策略将受信宿主 IM 身份绑定独立主体 runtime；捕获、分层读取、自动召回和 SQLite 降级共用授权，不用 session 非空冒充主人 | `tests/test_memory_identity_routes.py`：先复现未知用户能读固定主体，再验证双用户、群聊、环境伪身份、跨 profile、委派及撤销缓存/在途结果；当前与完整固定候选宿主均有实际 ContextVar/provider 加载契约 |
| 记忆配置故障关闭 | 配置解析失败、宿主 defaults/last-known-good 回退、错误配置段及损坏路由不回退 legacy/environment 命令；路由实例重新初始化不降级 | 四项配置反例先失败后通过，身份/配置专项 53 passed；真实宿主损坏 YAML 路径亦验证。不是 OS 租户沙箱，生产仍是 legacy 单主体 |
| 记忆 provider 安装 | 受管版本独立链接 localvec provider，不把它当插件启用；仓库实现不被旧外部快照覆盖；不完整目标在替换任何插件链接前拒绝 | `tests/test_superagent_installer.py`、身份路由安装及实际宿主 provider 发现；doctor/切换/回退/卸载均检查该受管链接，不隐式迁移旧个人库 |
| 路由记忆只读就绪工具 | 去重核对所有主体服务及保存的队列/身份/环境绑定；旧单主体服务活跃或无法查询时不算新路由就绪；策略损坏不采用 legacy | `tests/test_deployment_readiness.py`，先复现旧服务误判及配置异常，再与安装/身份/Worker/中立性专项共 88 passed；不打开数据库、不启动服务、不证明已加载 unit 或实际进程行为 |
| 研发验收命令授权 | 必跑检查取自管理员策略并在模型前冻结；任务不能新增或修改命令；验收持有同一工作树锁；沙箱模式缺执行器提前拒绝 | `tests/test_dev_runner.py`、`tests/test_verification_gate.py`；内部 TypeError 不重跑、不丢 env；真实 bubblewrap 文件/环境/网络隔离专项通过，但不是完整模型任务 containment |
| 验收资源边界 | 输出读取时按 stdout/stderr 各 1 MiB 限制，越界即停止而非结束后截断；超时清理不再无限等继承管道；运行错误不能匹配负退出码而过关 | `tests/test_verification_gate.py`；真实输出洪泛、脱离进程组的管道持有者、双管道 EOF 后仍运行、无效 UTF-8 和无效超时均有反例。不是任务全资源沙箱或后代停止证明 |
| 研发 CLI 适配器接线 | 管理员启动参数指定同步生成／验收模块，任务字段不参与导入；显式加载或执行失败不回退业务桥接，不重试；拒绝错误签名、异步函数、错误结果及未知/缩写参数 | `tests/test_dev_runner.py`：真实 CLI 生成代码、执行单测、产物回执；真实 bubblewrap 生成与验收隔离探针；Pool 运行 CLI 时两阶段取消/超时回收实际脱离进程组后代后仅一条终态回执。确定性测试适配器不是实际模型、生产 runtime 或 P2 整体验收 |

这些修复的回归先复现失败，再验证通过。它们不等于下列整体门禁已完成。

本地隔离检查命令：

```bash
python3 -m pytest tests/test_cardkit2_renderer.py \
  tests/test_package_superagent.py tests/test_superagent_installer.py -q
python3 -m pytest tests/test_durable_dispatch.py \
  tests/test_durable_dispatch_backfill.py tests/test_task_status_plugin.py \
  tests/test_liveness.py -q
```

打包、解压、正常 pip 构建、临时 profile 安装及 doctor 已实测通过；
这是使用合成宿主适配文件的离线检查，**不是实际 Hermes 启动证明**。
早期候选漏掉未跟踪新文件，已另用临时 Git index 显式纳入全部本轮文件重建，
没有改变用户真实暂存区。该候选包含 659 个文件；从解压源码 pip 构建、
临时 profile 受管安装、doctor，以及从包内安装/探测全部六项宿主保留补丁均通过。
这是 `publishable=false` 的历史 dirty 候选，不是正式发布物；本轮最后的候选
需在最终文档冻结后重建并独立核对。每次后续源码/文档变更都须重新构建，
不能复用旧归档的摘要。正式发布须在提交完整源码后重建并重验。

新增前台边界代码之前的完整回归：**2315 passed、62 skipped，退出码 0，耗时 372.92 秒**。
检查结束后重新收集为 2377 项，与上述通过及跳过总数一致，包含新加入的测试。
随后对当前文档、HFC 构建记录和单生产者门禁复验为 25 passed。
全量中默认跳过的 8 条 HFC 投递契约，另以记录对应的已安装包作源码，
在临时 HOME、替身平台和本地测试服务中执行，结果为 8 passed。
无真实 IM 外发、服务重启或生产记忆写入。

加入前台交接后的第一轮全量为 2386 passed、61 skipped、1 failed；失败为上述
记忆测试启动竞争，受控延迟监听已复现 `ConnectionRefusedError` 后修复。
修复及纵向链路扩展后的相邻专项为 **165 passed，耗时 162.68 秒**。
随后完整 `verify.sh` 为 **2388 passed、61 skipped，耗时 520.42 秒，退出码 0**，
防退化靶场另有 5 passed；真实渠道与模型端点阶段因未配置明确跳过，不算通过。
该结果在新增自动复盘门禁之前取得。门禁加入后的最终完整 `verify.sh` 为
**2390 passed、61 skipped，耗时 512.82 秒，退出码 0**；防退化靶场另有
5 passed，安装器 dry-run 退出码 0，真实渠道与模型端点仍明确跳过。
当时重新收集为 2451 项，与通过及跳过总数一致。文档/包/中立性/单生产者
专项另为 47 passed。
之后新增的正常独立进程恢复用例与 backfill/Worker 相邻专项为
29 passed / 1 skipped；不将它与先前全量拼成一次新的全量通过。
该全量现在是历史证据：之后增加了本地 attempt 监管、启动锁范围和人审修复，
不能继续把 2390 passed 声称为当前源码完整通过。
从 Git object 提取完整固定宿主源码、应用六项保留补丁后，使用临时 HOME
补跑插件注册、同事派发身份与收尾控制流，共 86 passed；直接交付和进度
回执另有 2 passed，平台传输为替身，无真实外发。这些补充证据不改变全量
日志中的跳过计数，也不代表实际部署、真实服务重启或客户端验收。
镜像测试依赖已统一读取 `pyproject.toml` 的 test extra；全新 venv 依赖安装
及 34 项独立策略/边界检查通过，但这不能替代 Docker 构建。

本地停止监管的第一轮全量为 **2409 passed、61 skipped、3 failed**，耗时
536.88 秒；不能算通过。两项失败揭示同事 runner 旧 token 清理会误杀带有
相同 token 的监管器，已改为只有任务子进程携带 token；另一项是旧测试仍要求
崩溃且未确认停止的同步执行器直接取消终结，已改为保留未知状态，并使用受信
所有者结束回调验证可终结的正向路径。失败场景和相邻测试已复验，但不拼成
一次新的全量通过。加入监管丢失投影修复之前的完整 `verify.sh` 结果为：
**2419 passed、61 skipped，耗时 495.58 秒，退出码 0**；防退化靶场另有
5 passed / 5.78 秒，13 个 shell 语法检查和安装 dry-run 通过。重新收集为
2480 项，与通过及跳过总数一致。真实通道、模型端点阶段仍明确跳过；这些
结果不代表现场启用或真实 IM 验收。之后投影修复的专项为 108 passed；再次冻结后
完整 `verify.sh` 最终为 **2419 passed、61 skipped，538.92 秒，退出码 0**；
防退化靶场另有 **5 passed / 6.10 秒**，13 个 shell 语法检查与安装 dry-run 通过。
重新收集为 **2480 项**，与本次总数一致。宿主补充专项以正确的
宿主解释器重跑为 118 passed；先前错误指定仅含本仓依赖的临时解释器时有 17 项
在宿主依赖导入阶段失败，不将这轮算通过或隐藏。新增本地候选需据当前源码重新构建，
候选摘要/安装证据保留在独立验证记录，避免文档变更后复用旧归档。

同版功能代码的启动取消/暂停、Worker 强杀与后代回收专项连续 5 轮、每轮
6 passed。HFC 部署记录、单生产者及投递契约另有 16 passed，传输仍为替身。
本地 dirty 候选包含 664 个文件，已核对两次归档一致、从解压源码 pip 安装、
离开源码目录实际运行安装包的监管器与 Worker、临时受管安装幂等及 doctor ready，
固定候选宿主的六项保留补丁应用/探测成功，包内相邻测试 57 passed。
这些是离线候选证据，不是现场服务切换、真实模型、消息平台回读或正式发布。
2026-11-03 初查 Docker 基础镜像时，daemon 的本地代理拒绝连接。后续通过
独立 rootless 容器工具拉取官方 Python 镜像，核对转换前后配置与全部根文件系统
层摘要一致，再载入本地 Docker 缓存；没有修改全局代理或重启 daemon。
构建仅使用命令级代理，检查容器使用 `--network none`、无宿主挂载或生产凭据。
首轮真实容器检查发现服务解释器默认路径及离线私有依赖缺失；修复后，下一轮
容器单测 **2354 passed / 135 skipped / 429.68 秒**，空 HOME 完整依赖安装、
离开源码目录导入、幂等及真实 Chromium 含图 PNG 渲染通过。该轮整体仍失败：
旧缺宿主断言没有接受安装器更早的“缺少 CLI”拒绝。断言已修正，须完整重跑，
不把这轮单测通过冒充干净室整体通过。带可选宿主源码的本机单测比容器多，
两者跳过计数不能互相填补。

这轮安装/容器修复后的本机完整自检还出现 **1 failed / 2427 passed / 61 skipped**
（608.53 秒），进度卡崩溃恢复用例未进入编辑窗口，因此不能继续把历史 2419
通过称为当前源码通过。单独和原模块重跑虽通过，进一步受控交错已复现上述
创建交接丢任务缺陷；修复后完整模块 53 passed。原崩溃窗口和受控交接反例
另连续 5 轮、每轮 2 passed。随后冻结功能代码的完整 `verify.sh` 为
**2429 passed / 61 skipped / 569.37 秒，退出码 0**，防退化靶场另有
**5 passed / 6.12 秒**，13 个 shell 语法检查与安装 dry-run 通过；真实渠道
和模型端点仍跳过。重新收集为 **2490 项**，与通过及跳过总数一致。

对应 dirty 候选共 665 个文件、`publishable=false`，两次归档字节一致；
从包内安装、离开源码目录运行真实监管器/Worker、临时受管安装幂等、doctor、
固定候选六项宿主补丁及包内 57 项专项通过。以此包源码构建并断网运行的
完整干净室检查 **退出码 0**：**2355 passed / 135 skipped / 400.45 秒**，
随后空 HOME 完整安装、包外导入、私有 Python 和真实 Chromium 含图 PNG、
幂等、身份合成及离线记忆引擎烟测均通过。仅构建时使用命令级代理，运行时
无网络、无宿主挂载、无生产凭据；不把此证据称为真实 Hermes 启动或 IM 验收。
日志分别为 `verify-opening-final.log`、`pytest-opening-final.log`、
`docker-opening-build.log`、`docker-opening-check.log`，保存在本机隔离验证目录。
本次记录更新之后的归档另行重建，并比对全部非文档输入不变；不能把旧归档
摘要冒充最终文档归档摘要。正式发布仍须完整源码入库后重新构建和核验。

当前宿主的旧路由 API 用例单独通过，完整固定候选的新版插件 API 用例也单独
通过。交叉套用时分别出现缺少 `scoped_session_key` 与 `expect_edits` 元数据
断言失败；这是版本/API 范围不一致，保留失败记录，不宣称两种宿主 API
任意互换或全矩阵通过。两条正向用例均使用临时 HOME 和替身网络传输。

### 记忆身份路由后的最终离线验证

两主体的捕获、分层读取、自动召回、降级查询、撤销与独立 Worker 计划已完成
隔离验证；安装器不覆盖仓库 provider 的修复也在同一功能输入中。配置故障关闭
的最后四项反例加入后，重新收集为 **2531 项**，没有将中途测试结果相加当作全量。

| 检查 | 当前功能输入的实际结果 | 证据范围 |
| --- | --- | --- |
| 本机完整 `verify.sh` | **2470 passed / 61 skipped / 574.79 秒，退出码 0**；防退化靶场另有 **5 passed / 8.06 秒**，13 个 shell 语法检查及安装 dry-run 通过 | `verify-memory-identity-final.log`、`pytest-memory-identity-final.log`；真实通道和模型端点明确跳过，不算通过 |
| 断网容器完整检查 | **2394 passed / 137 skipped / 418.72 秒，整体退出码 0**；空 HOME 安装、包外导入、私有 Python、真实 Chromium 含图 PNG（24793 字节 / 1 页）、幂等 275 项、身份合成及离线记忆烟测均通过 | `docker-memory-identity-final-build.log`、`docker-memory-identity-final-check.log`；运行时 `--network none`、零宿主挂载、无生产凭据。两项实际宿主身份契约在无宿主容器里明确跳过 |
| 功能冻结候选 | **668 个文件、`publishable=false`**；两次归档字节一致、逐文件比对当前源码，pip 包外运行真实监管器/Worker，受管安装幂等及 doctor ready，完整固定候选六项保留补丁通过；包内专项 **92 passed / 2 skipped / 17.31 秒** | `candidate-memory-identity-final-functional.log`；未启动生产服务或真实模型，不是正式发布物 |

功能冻结归档 SHA-256 为
`ededa12435cfb5260c471d4a4764fefab111291ed6c9deb6634c97abadb67e0e`，
对应镜像为 `super-agent-check:5f5fd1a-dirty-fbb3e481c655`，镜像 ID 为
`e6bf8c0c79a0ec4387b3309384aeb49b803eeb92898858d8ed3982b919c1240e`。
此处摘要只指上述功能冻结输入，不指包含本段最终验收文字的后续归档。文档归档
需重建并比对非文档文件完全不变，其摘要单独记录，避免自引用或冒用旧归档。
验证日志保存在本机 `/tmp/sa-release.WmN3kM/`，不是已提交的 CI 运行记录。
两种测试环境的跳过计数不互相填补；该轮冻结功能代码对应上述各自完整运行，
不覆盖下节之后加入的就绪工具扩展。

### 路由就绪工具后的最新完整验证

之后补齐的只读工具核对去重后的全部主体服务、保存的队列与身份/环境配置，
不把活跃的旧单主体服务算作新路由就绪。策略/配置重复键及类型错误明确失败，
systemd 查询错误不冒充旧服务已停；unit 额外命令、重复环境或本地覆盖保留 OPEN。
先复现 8 项新路由缺口及 6 项配置/查询失败，再修复；安装/身份/Worker/中立性
相邻专项为 **88 passed / 20.67 秒**。检查未打开记忆数据库、启动服务或消费队列。
它只验证保存文件和服务状态，未核对已加载 unit/实际进程环境，更不能证明模型
质量或真实 IM 身份传输。不能把 CONFIGURED 当作实际运行验收。

上一轮功能冻结后重新收集为 **2553 项**，完整结果如下；不与历史运行拼接计数。
这些结果早于下述同事绑定配置修复，不再表示当前工作树完整通过：

| 检查 | 最新功能输入的实际结果 | 证据 |
| --- | --- | --- |
| 本机完整 `verify.sh` | **2492 passed / 61 skipped / 576.33 秒，退出码 0**；防退化靶场另有 **5 passed / 6.97 秒**；13 个 shell 语法检查及安装 dry-run 通过 | `verify-memory-readiness-final.log`、`pytest-memory-readiness-final.log`；真实通道和模型端点仍明确跳过 |
| 断网容器完整检查 | **2416 passed / 137 skipped / 381.69 秒，整体退出码 0**；空 HOME 安装、包外导入、私有 Python、真实 Chromium PNG（24793 字节 / 1 页）、幂等 275 项、身份合成及离线记忆烟测通过 | `docker-memory-readiness-build.log`、`docker-memory-readiness-check.log`；运行时断网、零宿主挂载、无生产凭据 |
| 冻结功能候选 | **668 个文件、`publishable=false`**；两次归档一致，逐文件核对工作树，pip 包外运行真实监管器/Worker，受管安装幂等、doctor ready、完整固定候选六项保留补丁及包内 **116 passed / 2 skipped / 19.15 秒** | `candidate-memory-readiness-functional.log`；两项跳过为缺宿主源码的实际宿主契约，不是默认视作成功 |
| 补充门禁 | 包/中立性/单卡生产者 **35 passed / 1.68 秒**；HFC 本地构建/单生产者/投递契约 **16 passed / 1.81 秒** | `readiness-package-neutrality.log`、`hfc-readiness-final.log`；HFC 使用临时 HOME 和替身传输，无真实外发 |

最新功能冻结候选 SHA-256 为
`8af75d7d691b61687a1717f45bcf13e3aa81375d69051e2c00a4d9b97254b734`，
对应镜像为 `super-agent-check:5f5fd1a-dirty-3bd90f1cd544`，镜像 ID 为
`0d46e3be5f1e9f126a254f4b3335c8ec8e745c47b2b88b2b2d33a9c29aa6f075`。
这是本段最终文字加入前的功能冻结归档；文档归档另重建、核对唯一文档变化并
单独记录摘要。用户真实 Git index 保持未改；dirty 候选不作为正式发布物。

其余跳过包括可选原生调度绑定、需专门宿主解释器/配置的集成、
浏览器依赖、真实 IM 凭据及其他可选 HFC 用例；不得把这些算作通过。
本轮使用带文档测试依赖的隔离解释器，禁用真实发送凭据，并显式指定
不存在的模型配置，避免运行全量检查时触发真实模型探测。

### 同事会话绑定配置修复（2026-11-03）

静态绑定文件仍由宿主管理，不能由任务文本选择成员、代次、profile 或会话。
本轮先复现 12 项 JSON 重复身份字段（含转义键名）被静默覆盖的反例，
再改为拒绝歧义配置；错误不回显字段内容。另用受控文件替换复现两项读取竞争：
权限校验后替换为符号链接或公开文件，原实现仍读取并接受新绑定。
现改为打开时拒绝符号链接，对实际打开的描述符核验常规文件、所有者和私有权限，
读取最多 65537 字节并拒绝超过 65536 字节的文件。
不改变合法绑定、退役排空、既有任务归属和执行器 containment 门禁。
这不是恶意同 UID 进程的安全沙箱，也不证明所有同事路径已上线。
修复后的同事目录/派单/绑定/交付及包/安装相邻专项为
**152 passed / 1 skipped / 73.93 秒**。完整 `verify.sh` 为
**2506 passed / 61 skipped / 588.83 秒，退出码 0**，重新收集为 **2567 项**；
防退化靶场另有 **5 passed / 7.06 秒**，13 个 shell 语法检查和安装 dry-run 通过。
真实通道和模型端点仍明确跳过。

冻结功能候选含 **668 个文件、`publishable=false`**；两次归档一致、逐文件与工作树
核对通过，包外运行监管器/Worker、临时受管安装幂等与 doctor ready、完整固定宿主的
六项补丁均通过；包内 **148 passed / 3 skipped / 27.22 秒**。证据为
`candidate-colleague-binding-functional.log`，归档 SHA-256 为
`a4c00e52c49218f6c2a17599238ca842fe1feed853b5aecf9914fd85667949be`。
同功能镜像 `super-agent-check:5f5fd1a-dirty-02feedf500c5` 的 ID 为
`fd177976c1dabc38d30eb346db67ed2e4c7390a4d750876ed969bbbc3c1b7315`；
断网、零宿主挂载的完整干净室为 **2430 passed / 137 skipped / 387.30 秒，整体退出码 0**，
空 HOME 安装、包外导入、私有 Python/真实 Chromium PNG（24793 字节 / 1 页）、
幂等 275 项、合成身份及离线记忆烟测通过。

完整运行启动后又加强了一个测试输入：符号链接目标自身使用私有权限，排除因公开权限
而拒绝的假阳性。执行代码未变，当前绑定加载专项 **16 passed**；上述冻结归档/镜像
仍含旧测试输入，不把它们冒充最终文档包。最终包需重建，并核对差异仅为该测试及本文档。
完整日志见 `verify-colleague-binding-final.log`、`pytest-colleague-binding-final.log`、
`docker-colleague-binding-build.log`、`docker-colleague-binding-check.log`。

### 研发验收授权接缝（2026-11-03）

任务 payload 中的验收命令原会在模型返回后直接在宿主运行，且验收时工作树锁已释放。
本轮先用 12 个反例复现未授权命令执行、策略检查可省略、模型回调可修改检查和无效
策略仍执行模型，再改为管理员策略定义必跑检查、任务声明只可匹配、执行前冻结契约，
并把验收保留在同一个工作树锁内。`sandbox=True` 的检查必须使用宿主注入执行器，
没有适配器则提前拒绝，不降级宿主执行；CLI 当前不具备该适配器。

另复现执行器内部 `TypeError` 导致去掉 env 再执行一次、旧接口静默丢 env、假值
执行器被替换为宿主执行器及 NaN 超时未拒绝的问题，再逐项修复。
真实 bubblewrap 验收接缝专项为 **1 passed**：生成文件的两项单测通过，同时无法
读取宿主测试文件、继承测试秘密环境值或访问宿主 loopback 监听端口。模型为确定性
回调，仅验证验收接缝；不是实际模型任务或完整任务级停止证明。
配置与迁移说明见 `POOL_RUNTIME.md` 的 Development verification authority。

修复后相邻专项为 **162 passed / 3 skipped / 50.84 秒**。完整 `verify.sh`
退出码 **0**，源码单测 **2540 passed / 61 skipped / 579.21 秒**，重新收集
**2601 项**；防退化靶场另有 **5 passed / 7.31 秒**，13 个 shell 语法检查、
安装 dry-run 通过。真实 IM 交付与模型端点检查仍明确跳过。

冻结功能候选含 **668 个文件、`publishable=false`**，两次归档一致且逐文件与工作树
核对通过；正常 pip 安装后从包外运行监管器/Worker，临时受管安装幂等与 doctor
ready，完整固定宿主六项补丁均通过；导出源码专项 **219 passed / 5 skipped /
27.25 秒**。证据为 `candidate-dev-authority-functional.log`，归档 SHA-256 为
`e6d32acc03b2761ab1b9f1993c1ea4cbf2614643315dded96c0a6a740212e0ed`。
同输入镜像 `super-agent-check:5f5fd1a-dirty-e28e23cb16d5` 的 ID 为
`6f048845acf4bba94579da218fe9ad1c48c7ab0cc83a8f6c4e7dd4290a937548`。
断网、零宿主挂载的完整干净室为 **2463 passed / 138 skipped / 387.14 秒，
整体退出码 0**；空 HOME 安装、包外导入、私有 Python/真实 Chromium PNG
（24793 字节 / 1 页）、幂等 275 项、合成身份及离线记忆烟测通过。
容器没有 bubblewrap，该项明确跳过；不能用容器结果替代上面的宿主隔离专项。

另执行一次临时离线取消实验：真实 Pool Worker 进入 bubblewrap 验收后再请求取消，
停止确认后为 `cancelled`、`execution_state=supervised`，只有一条终态回执且未生成
成功报告。模型为确定性回调，实验未纳入自动回归，不是实际模型/IM 或恶意任务
完整停止证明。日志为 `dev-verification-cancel-probe.log`。

完整日志位于本地验证目录 `/tmp/sa-release.WmN3kM`：
`verify-dev-authority-final.log`、`pytest-dev-authority-final.log`、
`docker-dev-authority-build.log`、`docker-dev-authority-check.log`。
上述功能候选不含本段最终验证文字；文档冻结后另行重建归档并核对内容差异。
上文同事绑定的结果现为历史证据。
`colleague_worker` 可信 containment 门禁保持关闭，不把命令策略或替身测试冒充 P2 完成。

### 研发验收输出与清理边界（2026-11-03）

验收原先用 `communicate()` 完整收集输出后才截断，内存占用没有输出预算；
超时清理时再次无限等待，脱离原进程组的后代只要保留管道便可拖住调用。
本轮先复现 stdout/stderr 越界后仍产生后续效果、输出越界的匹配退出码仍被批准、
无效 UTF-8 丢失真实退出码共四项失败，再改为并发、有界字节读取、越界停止及
有界清理。补充覆盖 EOF 后进程仍活跃、脱离进程组的管道持有者、执行错误匹配
负退出码、无效直接调用超时。验收/研发 runner 专项为 **82 passed / 2 skipped /
1.47 秒**。

一次真实无限输出实验在 **0.015 秒**内停止（退出码 -9），保留 stdout
**1048619 字节，含 43 字节截断说明**。这是一次局部观察，不是性能 SLA、
总进程资源配额或完整任务停止证明。默认本地命令仍可运行任意工作树测试代码；
可信 containment 门禁没有放开，真实模型/IM 验收仍未完成。
红/绿日志为 `verification-output-red.log`、`verification-output-green.log`，
并将旧执行器单独装入测试进程复验：0.2 秒超时仍等约 3 秒的管道持有反例失败，
见 `verification-pipe-holder-baseline-red.log`。新资源边界 11 项连续 5 轮均通过，
见 `verification-output-repeat.log`；取消、恢复和同事 Worker 相邻专项
**39 passed / 7.46 秒**，见 `verification-output-adjacent-stop.log`。

此前的功能冻结输入完整 `verify.sh` 为 **2551 passed / 61 skipped / 674.75 秒，
退出码 0**，重新收集 **2612 项**；防退化靶场另 **5 passed / 7.72 秒**，
13 个 shell 语法检查和安装 dry-run 通过，真实 IM 交付与模型端点检查跳过。
该结果现为历史证据：之后 eaa466e（同事 tool backend 与派工）和 d2e7233
（同事完成回执与受管部署恢复）继续改动源码，不能再用它声称当前 HEAD 通过。
日志为 `verify-output-bound-final.log`、`pytest-output-bound-final.log`。

冻结功能候选仍为 **668 个文件、`publishable=false`**，两次归档相同且全部文件与
工作树一致，真实 Git 暂存区未改；正常 pip 包外导入/监管器 Worker、临时受管安装、
幂等与 doctor ready、完整固定宿主六项补丁通过，导出专项 **230 passed /
5 skipped / 20.39 秒**。日志 `candidate-output-bound-functional.log`；
归档 SHA-256 为 `25e4291a7569181cb93b96b468929cb872e861212a27ae5c869a51795ff7934b`。
同功能镜像 `super-agent-check:5f5fd1a-dirty-92df55ce7c54` 的 ID 为
`d20398fa5e2943f5b24e7207dc17e8781344e7ec4ad9437d3cc5899b91c0ec6e`。
断网、零宿主挂载的完整干净室 **2474 passed / 138 skipped / 401.55 秒，
整体退出码 0**；安装、私有 Python/真实 Chromium PNG（24793 字节 / 1 页）、
幂等 275 项、合成身份与离线记忆烟测通过。
日志 `docker-output-bound-build.log`、`docker-output-bound-check.log`。

上述功能归档不含本段最终验证文字；文档冻结后另行重建候选并核对仅本文档变化。
结果不表示真实模型任务、生产 profile、真实渠道、多用户记忆质量或正式发布已验收。

## P0：当前真实链路与唯一职责

| 边界 | 当前证据 | 与目标的差距 |
| --- | --- | --- |
| IM 前台 | `durable_dispatch.register()` 暴露提交、查询、取消；候选宿主独立执行门禁、IM 命令检查与提交回合释放，见 `FRONTDESK_BOUNDARY.md` | 权限/交接子项已隔离验证，未生产启用；真实模型、IM 第二消息、Worker/交付/重启完整纵向验收未完成 |
| 任务主账 | `agentcore/pool.py` 持久化任务、attempt、租约、取消、origin 和 outbox；`DispatchService` 验证 profile/session 所有权 | `colleagues` 配置使用各自队列；不能声称已具备跨队列幂等或跨会话授权 |
| 只读执行 | `agentcore.analysis_runner`，由管理员固定 runner argv 和源文件范围；独立 Pool Worker 执行 | 仍需目标宿主、实际模型、独立服务及可插话的纵向验收 |
| 开发同事 | `colleague_registry` 提供身份；`dev_runner`/`colleague_dev` 存在实现与局部测试 | `colleague_worker._trusted_containment_available()` 仍返回 False；不能删门禁启用。开发岗位仍有业务桥接依赖 |
| 活体状态 | `agentcore/liveness.py` 为观察投影；状态工具不能自行调度；本地 runner 终态要求 attempt 绑定的进程树停止回执 | 本地隔离故障矩阵已验证，旧在途和同步崩溃仍未知；不支持旧/新执行器混跑。实际服务及可信研发沙箱停止仍待验收 |
| 交付出口 | `DispatchService` 持久绑定 origin；`completion_backfill`/`completion_delivery` 处理回执；宿主注入 transport | 不同交付模式须按 profile 选唯一所有者；未知发送不可盲重发；仍需真实渠道回读和重启核对 |
| 原生卡片/HFC | 原生组件未发现插件/transport 调用方；新增 lock 记录本地修补 HFC 构建；其身份检查与 8 条自动契约已复验 | 本地修补构建与上游 pin 的 hold 分开；不启用第二个生产者，不因自动契约通过声称真实交互已验收 |
| 记忆捕获与召回 | 显式 `identity_routes_file` 模式按宿主受信身份路由到独立主体 runtime；捕获、L1/L2/L3、自动/降级召回共用身份接缝；见 `MEMORY_IDENTITY_ROUTES.md` | 原生产仍为 legacy 单主体。外部数据库隔离不等于宿主所有 prompt/会话/原生文件隔离；真实多用户验收、团队审核发布和模型质量仍待完成 |
| 记忆准入 | `memory_autonomy.py` 提供候选提取、审查、冲突、索引与审计路径 | 合成测试不能证明真实模型的错误准入率、召回质量或生产已启用；不自动消费生产积压或晋升策略 |

依赖方向保持：宿主插件/执行适配器 → 核心 Pool、状态和记忆契约。
核心不导入 IM 或宿主 CLI。前台调用任务接口，不直接调用开发 runner；
后台输出事件与产物，由已绑定的交付出口发送。

宿主工具边界检查还发现：当前宿主的插件回调抛错/超时会转换成 block，
但工具执行器和 `model_tools` 在钩子基础设施异常时存在放行分支。
因此不能只凭一个 `pre_tool_call` 回调就宣称完成硬隔离。
新增候选补丁已用独立门禁验证直接工具、inline 工具、tool-search/代码执行、
委派/连接器和 IM 命令入口；包含 helper 缺失/导入失败/抛错、损坏配置、
并发 profile 和预热缓存反例。它不依赖上述插件钩子放行分支来授权。
仍不能把现场 profile 标为“薄前台已启用”，也不能据此解除 P1 其他出口。

## 整体出口逐项状态

| 门禁 | 当前状态 | 完成需要的证据 |
| --- | --- | --- |
| P0 盘点 | 服务/配置只读核对已完成，运行验收未完成 | 见现场拓扑快照；需明确切换目标，完成唯一发送者运行验证，不把服务 active 当作能力验收 |
| P1 薄前台只读闭环 | 未完成 | 工具硬隔离、接单结束回合、后台未完成时处理新消息、双身份隔离、重启恢复 |
| P2 受控研发执行 | 未完成 | 可信执行边界、真实模型变更及测试产物、补充/取消/超时和后代停止证明 |
| P3 同事配置 | 未完成 | 跨任务身份、权限撤销、实例代次；无业务平台依赖的通用执行路径 |
| P4 记忆 | 未完成 | 身份边界、安全反例、真实模型的准入/召回固定样本评测，以及授权的生产启用 |
| P5 真实 IM 灰度与回退 | 未开始 | 明确测试目标和窗口后验证真实消息、附件、回读、呈现、插话与回退 |
| 当前源码全量测试 | 整轮 `verify.sh` 退出码 0；真实运行仍未验收 | HEAD `e0a953a` + 同日修复的工作树 **2754 passed / 63 skipped / 709.91 秒**，防退化另 5 passed，⑥.1 三项延迟门禁全通过，⑦ 模型端点 1000000；⑥ 真实通道缺 `FEISHU_*` 按规则跳过，**不算通过** |
| 干净室 | 本轮功能候选通过，非正式发布提交 | 断网容器 2474 passed / 138 skipped / 401.55 秒，安装/浏览器/幂等/离线记忆通过；不是实际宿主/IM 验收 |
| 正式发布物 | 未生成 | 干净且包含全部新文件的提交、可复现归档、完整测试、安装/回退、对应能力的实际验收 |

不绕过 containment/readiness 门禁，不覆盖历史任务/记忆，不自动重放未知外部效果。
目标环境切换、服务重启、真实外发、生产记忆写入和积压消费需要明确目标与授权窗口。
