# 同事运行与长任务交互：本机进展

这次只使用本机 WSL 的 caixuf 环境和临时数据，没有访问 Emily，也没有改线上配置或发送测试消息。

## 同事运行

- 普通任务默认由一个完整 Agent 完成；只有适合独立并行的大任务才交给同事。岗位描述是工作方向，不能代替宿主权限配置。
- 新增宿主侧 Hermes 单回合执行和会话绑定原型。会话按成员、代次、用户 profile 和用户会话区分；同一成员的工作先串行，避免状态检查与执行并发穿透。
- 先持久登记工作，再创建会话。超时、异常或退出失败时保留占用，需宿主核实进程和副作用后明确处理，不自动重复执行。
- 本机临时 Hermes 数据根中，实际调用原生 Gateway 会话 API 验证了创建、按同一请求重试和恢复使用同一个 ID。该验证在进程内进行，没有执行真实模型回合；真实 socket Gateway 加 CLI 子进程恢复仍未验证。
- 执行器尚未接入生产插件、可安全运行真实 Hermes 的持久队列、真正的成员实例创建和最终消息投递，不能当作可用的完整团队系统。团队长期记忆共享仅在隔离库验证了基础存储边界，尚未完成各成员生产接线。
- 后续增加了专用 Pool 队列入口和 worker 协议：按会话及请求号去重，执行前重新授权，不自动重放，结果由宿主按原请求者查询，不直接发送。隔离测试通过模拟执行器验证了提交和结果归属。
- **真实 Hermes 的整场尝试范围已接入（原拒绝开关已替换为能力核验）**：Pool 认领任务时生成随机尝试令牌，放进 worker 环境；Hermes 单回合启动器在 `stdin` 上以完整 argv 启动（`start_new_session=True`），把同一令牌导出给整个进程树，回合结束/超时后遍历 `/proc` 按令牌找出全部仍存活进程（含 `setsid`、脱离进程组的后代），先 SIGTERM 再 SIGKILL，并以 `/proc` 复查证明为空。`ProcessResult` 现在必须携带 `quiescent` 与 `containment` 证据，否则 runner 拒绝结算；worker 另做一次同令牌复查，只有证明为空才输出成功回执，Pool 再把 `attempt_quiescent` 和证据摘要写进结果。没有证据时失败关闭，不输出成功。配置 `dedicated=true` 只是宿主承诺，不自动证明独占；接口接受的 owner 也必须由可信宿主从已认证会话绑定。
- 本机隔离试验表明，systemd scope 能回收在同一 scope 内调用 `setsid` 的子进程；但子进程再调用 `systemd-run --user --scope` 时，新 scope 与原 scope 同级，停止原 scope 不会停止新 scope。Hermes 可能使用这种方式启动后台进程，因此**只给外层加一个 scope 仍不够**。实验只创建和清理临时 scope，未碰机器人服务。
- 进一步核查发现，Hermes `chat --resume` 是 Gateway 的客户端，回合通常由 Gateway 侧执行；管住队列启动的 CLI 并不能保证管住实际执行。上游本地 Gateway + 模拟模型集成测试在临时数据根启动了托管 worker，但 `compression.history` 遭 `permission_denied`，任务没有成功。需核实 Gateway 的任务身份、停止和终态契约，不能把此测试当作模型执行通过。
- 同一临时 Gateway、loopback 模拟模型下，上游 `test_native_gateway_runtime_live` 实际通过：验证会话创建与恢复、`prompt.submit`、完成事件回放、同会话 FIFO、分支和删除。测试夹具显式给子进程设置 `HERMES_DISABLE_LAZY_INSTALLS=1`，避免对临时数据根反复准备依赖。该结果证明普通 Gateway 测试链，不证明 safe-mode、真实模型、消息平台或 super-agent 同事接线已通过。
- 队列检查执行器返回的请求号、发起会话、成员及代次，拒绝归属不一致的结果。Pool 记录 `completed` 只代表 runner 返回；对话侧目前显示 `result_unverified`，不把未解析的 CLI 输出当作已验收答案。
- 新增 Gateway 原生派工接口，直接提交稳定 `input_id` 并查询 `prompt.receipt`；断线重试时核对 Gateway、成员和会话身份，回执只提供经绑定检查的简要答复。Gateway 本身报告终态后，仍需宿主提供已绑定任务身份的后台工作结束证据，才释放同事占用。新旧派工入口通过 Registry 的持久独占标记互斥；旧版本已存在但没有独占标记的未结束任务需要人工核对。
- 查询结果带有 Registry 确认的成员名称、岗位和代次，不从模型文本猜执行者；过长的答复会标记已截断。Hermes 的 `process.list` 和 `subagent.list` 只是 session 范围的部分投影，空列表不能证明后台工作已全部停止，因此不能单靠它们自动释放占用。
- `colleague-team` 新增默认关闭的派工和状态查询工具；调用者身份由 Hermes 当前 profile 和 session 绑定，模型不能指定 owner。真实宿主必须先注册可信 dispatcher；现已有 bootstrap 组装接口，但还缺生产 Provisioner 和现有服务的启动接入，因此工具在未接通时明确返回不可用，不代表招聘后同事已上线。
- `tests/test_colleague_gateway_real_host.py` 在本机临时 Gateway 和 loopback 模拟模型中实际通过了新派工接口的提交、重复请求和结果查询；其中一项使用 Hermes 官方 ticket 连接，成员准备仍由夹具注入。这尚不代表生产的成员创建或消息平台结果投递已经接好。
- 宿主连接按 profile 分开，使用 Hermes 官方 ticket，只连接已就绪 Gateway。每次断线重连重新发现并核对同一 profile；临时 Gateway 重启后用原连接恢复会话的测试通过。Windows 的认证主体仍无法由当前适配器可靠确认，暂不开放该路径。

## FlowCoro

`super-agent` 已有可选 FlowCoro 调度后端，SQLite 是任务账本。caixuf 本机现有旧构建缺少 `CancelledError` 等所需接口，直接运行相关测试有 9 项失败。另在临时目录编译上游 `aaac885e673e01b6f21037a47930ef744668d26b` 后，调度器和 worker 的 25 项测试通过。该编译没有覆盖本机仓库或切换生产服务；测试证明兼容性，不证明业务提速。

FlowCoro Python 绑定执行 Python 回调时仍需 GIL。性能优化应先测量实际等待和 CPU 消耗；确认热点后再考虑把热路径下沉到 C++ 或独立进程。任务持久化、取消和外部副作用恢复依然需要明确处理。

## 长任务交互

实际运行的服务是 `hermes-gateway-caixuf.service`，配置来自 caixuf profile。当前 Feishu 工具进度、中途说明和 commentary 关闭，文字流式输出开启；长回合通知配置为 60 秒。流式输出只能展示模型实际输出的内容，不能保证用户看到任务阶段。60 秒通知是否在某次真实任务中成功送达，还需要针对该任务核对。

当前 `task-progress` 主要跟踪被识别的后台进程。10:03–10:12 的服务日志出现过成功发送和编辑进度的记录，但这不能证明普通前台长回合也有可见进度。当天网关连续重启并触发过自动恢复保护；不能仅凭这些日志断定某一条用户任务因此中断。

下一步应在现有长任务展示工作上补齐前台回合的可信事件，而不是再建一套卡片：接单后建立固定入口，记录模型等待、工具执行、后台/同事工作和终态；无新证据时显示等待或状态待确认，不编造百分比。重启后的恢复、消息去重、插话和终态交付须在 caixuf 的真实消息平台测试对话验收。不要为了应用配置变更而直接重启正在工作的网关。

前台回合的隔离实现已经加入同一个 `task-progress` 插件，并增加发送前意图、消息 ID 检查点和发送期间后台任务的持久待合并记录。模拟崩溃测试覆盖拿到消息 ID 后的卡片绑定和重注册恢复；平台已接收消息但本地尚未拿到 ID 时，仍只能标记待核对且不重发。真实消息平台发送和完整线上恢复尚未验证。

## 测试

使用本机临时编译的上游 FlowCoro，运行同事执行、会话绑定、成员管理、工作树、共享记忆、对话历史、调度器和 worker 定向测试：**109 passed**。这些是隔离测试，不能代替真实模型和消息平台端到端验收。

随后使用 Hermes 自带解释器、本机临时 Gateway 和可选 FlowCoro，合并运行长任务展示、真实插件宿主、同事工具与 Gateway、Registry/Provisioner、只读队列、会话与记忆隔离、worker 和调度器测试：**161 passed**。Gateway 测试使用 loopback 模拟模型，成员 readiness 和最终投递仍是隔离夹具，不能当作线上验收。

## 本轮补充：进度消息和同事状态

- 长任务进度绑定发起者的 profile 和会话，读取和结算任务前检查归属；旧记录缺少归属时显示待确认，避免跨会话展示。
- 更新只使用经过核查的原消息编辑路径。未知平台或不支持的内容不会尝试可能产生新消息的回退。消息平台实际发送和重启后的线上行为仍需真人验收。
- 每次编辑之前先持久记录发送意图；进程中断或编辑结果不明时冻结自动更新，避免重试覆盖较新的状态。明确被拒绝且确认未生效的编辑可安全重试。同一消息的编辑串行进行。
- 同事状态区分等待、处理中、回复后后台工作待确认和真正完成；只有明确成功且宿主确认后台工作结束才显示已完成。成员名称来自登记信息，不由模型猜测。
- 后台工作结束的可靠证据和运行中服务的 bootstrap 仍未接好；当前改动尚未部署到运行中的网关。
- 重启后可恢复的后台任务重新加入轮询，继续更新原消息；未知编辑结果仍冻结，不能盲目续写。
- 本机 Hermes 环境下合并运行长任务、真实插件宿主、隔离 Gateway、同事调度与 worker 相关测试：**130 passed**。真实宿主测试使用模拟传输，Gateway 使用模拟模型；不等同于真实消息平台验收。
- Gateway 同事答复支持在原会话分页读取，按答复版本校验，避免把变化前后的内容错误拼接。相关 dispatcher、状态与 Hermes 插件宿主定向测试 **17 passed**；功能仍依赖尚未完成的实际派工接线。
- 新增宿主 bootstrap，将登记库、Provisioner、成员会话绑定、Gateway 连接和派工工具组装并处理关闭。临时真实 Gateway 与 loopback 模型的纵切测试覆盖会话创建、派工、重复提交及结果查询；成员 ready 使用测试夹具，后台占用依然待确认。真实后端、自动部署和共享长期记忆接线仍未完成。
- `/longtasks` 和 `/longtask` 能查询本会话的前台回合，也能复制完整后台任务号；无法确认当前会话时拒绝查询。相关本机测试 62 项通过。此前一次真实插件宿主测试出现任务归属不匹配，之后单项连续 15 次及整组复跑通过；原因尚未查明，仍需关注。

## 本轮补充：整场尝试范围、清理证据与共享记忆常驻服务

- 新增 `agentcore/colleague_containment.py`：令牌只允许 32 位小写十六进制；按 `/proc/<pid>/environ` 匹配而不信任进程名或进程组；排除宿主自身；对已退出但未回收的僵尸按已结束处理；证据摘要由令牌和有序信号/存活列表计算，不可伪造。
- 隔离测试实际验证：`setsid` 子进程在另一会话中，`killpg` 到不了；带令牌扫描能找到；清理后进程消失且扫描为空。跨进程纵切测试用假 Hermes 可执行文件在回合内 `setsid` 逃逸并写下 pid，队列 worker 执行后该 pid 已不存在，任务完成、工作占用归零、证据摘要为 32 位十六进制。
- 共享记忆新增宿主侧 `MemoryRuntimeSupervisor`：用绑定库固定的 argv/环境启动常驻 runtime 服务（`start_new_session=True`、日志 0600、`O_NOFOLLOW`），同一 profile 重复启动直接报错；就绪以队列文件出现判定；停止先 SIGTERM 进程组、超时再 SIGKILL，然后用同一尝试令牌复查，返回码与清理证据一并返回。测试实际启动并停止真实 runtime 服务，退出码 0、无残留、日志权限正确。
- 已知边界：令牌扫描依赖 `/proc`，只适用于 Linux 且同一 PID 命名空间；容器或 systemd 分离命名空间里仍需宿主侧的 cgroup/scope 配合。systemd scope 的结论不变：新 scope 与原 scope 同级，停原 scope 不会停新 scope，因此 scope 只能作为补充而不是唯一证据。

## 本轮补充：真实消息渠道（不再只靠替身和真人验收）

- 新增 `agentcore/feishu_transport.py`：把此前只有测试替身的交付 `transport` 换成真实消息平台开放平台调用。按任务 `origin.chat` 发 `msg_type=text`，发完立刻 `GET /im/v1/messages/:id` 回读，只有会话一致且文本摘要一致才返回回执；回读不符时本地记 `unverified` 并抛 `feishu_receipt_mismatch`，绝不重发。
- 至多一次：同一 `delivery_key` 既作为平台 `uuid`（去重窗口内挡重复），也写本地 0600 SQLite 日志；键复用但文本或会话变了直接 `delivery_key_conflict`，不覆盖；`lookup` 只查不发；`recall` 撤回并把状态记为 `recalled`。凭据只经 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 进内存，异常里只出现分类码。
- 两条交付缝各自接通：只读最终答复走 `ReadOnlyDelivery`（要 `send_plaintext`/`lookup`，本传输原生就是该契约）；持久任务的完成回执走 `DirectCompletionTransport`（要 `sender(address, item, *, idempotency_key)`）。后者由 `FeishuCompletionSender` 适配：只有“回读确认成功”才回 `{'ok': True, 'message_id': …}`；只有**尚未触网**的拒绝（地址无 chat、文本不可用）才回 `not_sent`，允许安全重试；其余一律不表态，让队列保持 `ack_unknown`，不冒第二次发送的险。附件未实现，直接抛 `feishu_file_delivery_unsupported`，不无限挂起。
- 离线测试 `tests/test_feishu_transport.py`（19 项，替身服务）：回读不符不交付、已记日志但回读不成立时抛 `prior_delivery_unverified` 且不发第二次、键冲突、撤回后 `lookup` 为空、平台错误分类、日志文件 0600。
- 真实测试 `tests/test_feishu_transport_real.py` 与 `tests/test_feishu_delivery_real.py`：缺凭据时 skip。真实跑通的是完整交付决策链——`ReadOnlyDelivery` 判定该发 → 真发 → 平台回读 → 队列 `acknowledged=1`、成员 `tracked_open=0`，再调一次 `deliver` 不重发，最后撤回。连跑 3 次全过。
- 独立客户端复核：`lark-cli im +chat-messages-list --as bot --chat-id oc_…` 能看到该文本消息（`msg_type=text`、正文一致），撤回后同一条变 `deleted: true`、`lookup` 返回 `None`。这条复核不经过本仓代码，证明消息确实落在平台上。
- 抓到一个会被误当成“平台抖动”的坑：任务 ID 由 profile/会话/请求 ID 派生，测试若用固定请求 ID，重跑时 `delivery_key` 不变、平台 `uuid` 去重返回**上一条已撤回消息**的 ID，回读必然不符。这不是传输层缺陷（它正确地没有再发一份），但会让“重跑真实测试”假红；真实测试已改用每次新的请求 ID，并把这个原因写在测试注释里。
- 已知边界：`literal_text_only=True` 的宿主声明只对 `msg_type=text` 成立（该类型不解析 Markdown，且本模块从不生成 `<at>` 提及）。消息长度上限按 UTF-8 30KB 拒绝；更长的结果仍需附件或分片，不在本轮。真实用户会话里的最终呈现仍未验收：未经用户明确指示，不代发消息。
- 自检入口：`verify.sh` 新增第 ⑥ 块，凭据齐全时跑两个真实测试，缺凭据记入“跳过（环境缺失，不算通过）”。

## 交接：2026-10-29

当前工作在本机 WSL `super-agent`，工作树有多项未提交和未跟踪文件，包含并行工作的改动；不要重置或覆盖。没有部署到运行中的 caixuf Gateway，也没有在 Emily 上测试或发送消息平台消息。

- 共享记忆新增宿主绑定原型 `agentcore/colleague_shared_memory.py` 和 `agentcore/colleague_memory_cli.py`。隔离测试使用 Hermes 的 localvec 提供器加载并验证同团队召回、跨团队不命中；实际 runtime worker 的单次运行验证了回合进入独立队列。它还没有安装到 Hermes profile，也没有验证真实 Gateway 模型回合里的自动召回或完整自动写入。
- 共享记忆映射由宿主管理，新增成员须核对版本及登记库；现有 profile 不能删除或改绑。profile 绑定不是同 UID 任意代码的访问控制，已经启动的退休成员 worker 需要宿主停机或增加运行时校验。
- 最新定向验证：长任务及真实插件宿主一组 62 项通过；Gateway bootstrap、派工和插件宿主一组 26 项通过；共享记忆及长任务查询定向一组 10 项通过。这些测试有交集，不能相加当作独立覆盖。临时 Gateway 使用 loopback 模型，就绪使用测试夹具。
- 接下来优先处理真实成员启动与就绪证明、成员退休及后台工作结束的可信证明，并在真实 Hermes profile 上跑通一次已接线回合；共享记忆的配置文件和常驻 runtime 现已能由宿主启动、就绪和停止，但尚未安装到 Hermes profile 或接入生产宿主启动流程。之后才能在本机 caixuf 隔离 profile 做真实模型和消息平台验收；不要用 terminal 回执或空进程列表冒充后台已结束。
- 长任务需继续复现上述偶发会话归属异常，并验证实际消息平台的进度卡更新、重启恢复、插话与终态。用户此前要求自己发送消息平台测试消息，未经明确指示不要代发。

## 仓级门禁与注入边界（收尾）

全量 `pytest tests`：**1433 passed, 23 skipped**（跳过项全是环境/可选依赖：`HERMES_SOURCE` 未设、`flowcoro_py`、`websocket-client`、原生 binding 未装）。

- 业务字面量棘轮原先红在进度报告、插件 README 与 `verify.sh` 里出现的具体业务产品名。按门禁要求统一换成占位词「消息平台」，两道具名性门禁 25 项通过（含本轮报告改动）。真实传输模块名与 ASCII 标识（`feishu_transport`、`FeishuTransport`）不在词表内，保持不变。
- import 边界门禁原先红在 `agentcore` 的 3 处宿主 import。现在这些依赖全部改成**注入**，`agentcore` 里不再出现 `hermes_cli` / `hermes_constants` / `gateway`：
  - `HermesGatewayHost(profiles, connector=...)`；真实建连搬到宿主侧 `plugins/colleague_team/host_runtime.py:connect_profile`，返回 `(GatewayConnection, 期望的 gateway profile id)`。
  - `HermesGatewaySessionCreator(..., store_factory=...)` 与 `ColleagueGatewayBootstrap(..., session_store_factory=...)`；宿主用 `host_runtime.session_store`。
  - `colleague_gateway_service.configure_session_env(get_session_env)`；插件在启用 Gateway 派工时调用。没有它就无法判断当前是否 cron 会话，派工**失败关闭**。
- 三条拒绝路径都显式报错而不是猜：`gateway_connector_required`、`session_store_factory_required`、`session_env_provider_required`。新增两条测试钉住前两条；第三条由插件的宿主装配路径覆盖。
- 用真实 Hermes 模块核对了签名兼容性（只 import、未建连）：`GatewaySessionStore(connection=None)`、`connect_profile(profile, home)`、`session_store(connection)` 与注入形状一致。
- `tests/test_arch_doc.py` 的浏览器端到端那条原来只在缺 Chromium 时 skip，本机有 Chromium 但缺 `websocket-client` 时会报错。可选依赖缺失不是渲染链故障，现在两者任一缺失都 skip。
- 本轮没有新增生产接线：Gateway 派工仍缺生产 Provisioner backend，也没有在现有服务里启动 bootstrap。以上只是把「引擎不依赖宿主」变成可验证的事实。

## 补记：尝试级 containment 从「静默拒绝」变成可验证契约

此前 `agentcore/colleague_worker.py` 遇到真实 Hermes 会直接以 `hermes_attempt_containment_not_integrated` 退出——把「无法证明工作已停」写死成「不可用」，且没有任何用例盯住这个行为。现在拆成两层，细节写进 `docs/COLLEAGUE_UNIFICATION_GUIDE.md` 第 2 阶段第 5 条：

- `_require_attempt_containment()`：所有 runner 必须自报 `attempt_containment`，只有 `runner_and_descendants` 放行。`gateway_delegated`（内置 `hermes chat` 启动器）、`host_owned`（宿主注入启动器）以及**不报值的 runner** 一律在建立预留之前拒绝，错误为 `colleague_turn_containment_unprovable;<kind>`；对外回执仍是统一的 `colleague_execution_failed_or_unknown`，细节不泄漏给队列。
- 原写法只对 `isinstance(runner, HermesColleagueRunner)` 检查，非 Hermes 的启动器可以直接绕过——这个缺口已删除，改为对所有 runner 统一取值。
- `_attempt_evidence()`：成功后用队列下发的 `AGENTCORE_ATTEMPT_TOKEN` 再扫一次 `/proc/<pid>/environ`；不静默就整体失败（`attempt_not_quiescent`）。没有令牌或令牌非法时回执的 `quiescence_evidence_id` 为 `null`，不假装拿到了证明。
- `HermesColleagueRunner.run()` 在 `gateway_delegated` 时于入口直接抛错，不再先建预留再失败。
- 失败路径也清理：`_discard_attempt()` 在任何异常之后用同一个令牌扫一遍，避免崩溃把逃逸进程留到下次重启；它不产出证据、也不改变统一的失败回执。

新增 `tests/test_colleague_worker.py`（9 项）和 `tests/fixtures/colleague_worker_probe.py`：拒绝内置、宿主注入、不表态三种启动器；无令牌与非法令牌得到 `null` 证据；合法令牌产出 32 位十六进制证据；一个真正 `setsid` 脱离进程组的后代被令牌定位并终止；失败回合同样清理遗留后代；未定义的队列信封被拒。`tests/test_colleague_worker.py` 单独复跑 9 passed。

**仍未完成的是 runner 本身**：仓库里没有任何实现声明 `runner_and_descendants`。也就是说本机现在跑不了真实的同事回合，这是显式缺口而不是静默失败。要用真实 Hermes，需要在宿主进程内构造 agent（而不是 `hermes chat` 子命令），由它回答「这个 attempt 派生出的进程都停了」。共享记忆的宿主绑定同理：`agentcore/colleague_shared_memory.py` 已经能给出 worker 的 bootstrap 与令牌环境，但还没有装到任何 Hermes profile，真实 Gateway 回合里的自动召回与写入仍未验证。

## 合入 master 时的边界说明

- 本机全量回归：1517 项通过、7 项跳过、2 项失败；失败来自测试夹具未声明新的执行隔离能力。补齐纯测试夹具后，同事派工、worker 和隔离相关回归 22 项通过。此次没有再次完整运行全量回归。
- 进程环境变量标记扫描不能证明共享 Gateway 启动的子进程已结束，也不能发现移除标记的子进程。当前直接 worker 对无法证明归属的 Hermes 执行拒绝运行；真实执行的生命周期隔离仍需实现并验证。
- 未运行真实消息平台发送测试，也未在 Emily 部署或验收。共享记忆、真实业务派工以及生产启用仍需独立验收。
