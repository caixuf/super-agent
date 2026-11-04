# 前台权限边界（候选宿主）

更新日期：2026-11-03（Asia/Shanghai）。状态：隔离实现与测试，未部署、未启用生产 profile。
这是 P1 的工具权限与回合交接子项，不是完整的前后台闭环或研发执行沙箱。

## 架构决策

前台权限归宿主适配层，不归提示词、模型参数或任务核心。
`patches/patch_frontdesk_boundary.py` 将
`runtime/hermes_frontdesk_policy.py` 安装为候选宿主根部的
`superagent_frontdesk.py`，并在已有派发接缝调用它。
宿主接缝 → 权限 helper → 受信 profile 配置；helper 不导入任务核心、
不执行模型调用，`agentcore` 不反向依赖宿主。
`durable_dispatch` 仍只负责持久任务接口，不承担工具防火墙。
`runtime/hermes_frontdesk_turn.py` 安装为 `superagent_frontdesk_turn.py`，
负责本轮工具回执到接单确认的确定性转换；宿主回合收尾调用它，
它只查询同一受信权限 helper，不提交任务、不发消息、不调模型。
仅隐藏 schema 无法阻止直接调用；仅插件 pre-hook 无法覆盖 inline 工具，
且钩子基础设施故障可能放行，因此保留独立的执行前检查。
代价是需要显式宿主补丁、锁定源码接缝和候选行为回归；源码漂移拒绝安装，
不靠关闭旧门禁或隐式修改正在运行的宿主来“兼容”。

## 可信配置与权限

在前台专用 profile 的 `config.yaml` 中显式配置：

```yaml
superagent:
  frontdesk:
    enabled: true
    memory_read_enabled: false
```

后台 Worker 使用另一个 profile，不开启该开关。不要让后台执行器继承前台
profile 后再尝试绕过门禁。本补丁不创建、迁移或修改任何 profile 配置。
配置与工具集修改在新会话中启用，不热换已有会话的提示词或工具 schema。
执行检查每次读取当前 profile 的策略；既有会话中的旧 schema 不构成权限。

固定允许 `durable_submit`、`durable_status`、`durable_cancel`、`task_status`。
这只是权限上限，不会注册工具，也不保证任务服务可用；任务接口仍须检查
受信 profile/session 身份、路由和管理员 source 范围。
前台不能调用 terminal、代码执行、文件工具、委派、未知插件、连接器、
tool-search 及其间接调用入口；配置不能添加任意工具名来扩大白名单。

只有 `memory_read_enabled: true` 才额外允许 `memory_search_l1`、
`memory_timeline_l2`、`memory_get_observations_l3`，仍不允许记忆写入。
这**不是多用户记忆隔离**；在受信身份/记忆范围未验收前保持关闭。

IM 只允许 `help`、`commands`、`whoami`、`version`、`new`、`reset`、
`stop`、`status` 控制命令。未知命令、执行快捷命令、别名和 skill 命令
在扩展前拒绝。普通聊天不受命令白名单限制；任务取消仍通过受权任务 API，
不能把 `/stop` 聊天回合当作后台任务已停止的证明。

## 执行接缝与故障语义

| 接缝 | 检查位置 |
| --- | --- |
| `model_tools.handle_function_call` | tool-search bridge、middleware 和真实派发之前 |
| `agent.tool_executor._dispatch_authorized_once` | 共享顺序/并发授权路径，覆盖 inline 工具 |
| `agent.agent_runtime_helpers.invoke_tool` | 即使调用方已标记跳过 pre-hook，仍独立检查 |
| `gateway.run_inbound._handle_message` | admitted event 之后、HFC/待审批/忙会话/快捷命令路由之前 |
| `model_tools.get_tool_definitions` | 未缓存与缓存返回均按当前调用 profile 过滤 |
| `agent.turn_tool_round.run_tool_round` | 本轮工具结果持久化及 guardrail 检查之后、压缩或下次模型调用之前 |
| `agent.turn_finalizer.finalize_turn` | 将 `frontdesk_handoff` 作为正常交互结束，不将接单当作后台完成 |
| `AIAgent._spawn_background_review` / `_spawn_background_review_now` | 模型路由、延迟排队或启动线程之前，按相同权限拒绝自动、显式及延迟恢复的记忆/技能复盘 |

IM 命令读取宿主为 admitted source 解析出的 runtime profile，不借用
环境变量中的启动 profile。多 profile 进程在没有 context-local home
绑定时拒绝工具执行。并发测试包含 Worker 与前台交错调用，以及预热的
共享原始 schema 缓存，验证 Worker 权限不被前台污染、前台不借用 Worker 权限。

配置格式损坏、重复键、未知 frontdesk 键、非布尔开关、超大/非普通配置文件，
或者 helper 无法加载（例如首次导入时文件丢失）、导入失败、检查抛错，
均拒绝执行并返回固定错误，
不向 IM 复制配置内容或解析异常。helper 故障时，原本允许的任务工具也拒绝。
有效的未启用配置或缺少 `config.yaml` 表示未选择该模式，保留原宿主行为；
**没有“曾经启用后配置消失仍锁定”的持久 latch**。
解析采用宿主依赖 ruamel.yaml 的 YAML 1.1 语义，不另依赖 PyYAML；
每次检查创建独立的安全 parser，并拒绝重复键。
配置是管理员的受信输入，本机制不是针对管理员/宿主插件或进程内任意 Python
代码的安全沙箱；不防止可信插件在自己的回调中直接实施副作用。

schema 过滤覆盖上述工厂返回；记忆/context provider 可能随后附加 schema，
不能据此声称每个最终 API 请求的 schema 都已过滤。执行前门禁才是权限依据。

前台即使显式允许记忆读取，也不能自行启动宿主的记忆/技能写入复盘；
入口及延迟执行入口均检查，helper 缺失、策略损坏或检查异常时不启动。
Worker 未启用前台策略时保留原复盘行为。不修改全局复盘配置，也不清除已有队列。
外部记忆 provider 的回合捕获/异步 spool 并未因此关闭，它是独立的受信宿主
集成路径，不等于模型获得记忆写工具，也不代表已完成多用户隔离或准入验收。

## 提交后释放前台回合

本轮包含 `durable_submit` 时，宿主只检查本轮新增、call ID 对应的工具结果。
成功回执必须是 `ok: true`、合法任务 ID 和主账状态；不用模型输出、
历史工具结果、状态查询或 `next_action` 字段推断“接单成功”。
前台专用 profile 直接生成有任务 ID、状态与角色的确认，追加正常 assistant
收尾并走原宿主 finalizer/发送路径，不再请求模型做总结、不轮询后台。
最后一个工具预算回合也能正常结束，下一条用户消息不继承“前台仍在等任务”。
后台任务和前台回合的完成语义保持分离。

失败、超大、重复、损坏或缺失回执不确认接单，提示保留原请求键并查询核对，
仍结束本轮，防止模型立即换键重试。混合批次保留已确认 ID、单列不确定项。
交接 helper 无法加载或检查抛错时标记前台回合失败，保留结果可能已持久化的
提示，不把未知提交当作未接单。工具结果持久化失败与 guardrail halt 优先，
不走成功确认。未启用该模式的 Worker 保持原来的模型工具循环。
本轮已经发出的工具 batch 仍按宿主原规则执行/收集；不是中途取消 batch 的机制。

隔离测试通过真实 AIAgent 工具循环、持久队列及只读 analysis_runner 子进程，
只在模型响应/外部网络边界使用替身。只调用一次前台模型即可接单结束，
后台模型边界保持阻塞、真实任务为 running 时，同 Agent 能处理下一条聊天。
同请求重试保留原任务 ID；已知任务 ID 也不能跨 profile/会话查询或取消。
执行完成后重开队列、切换当前聊天，由唯一替身 transport 消费 outbox，
结果仍投往原持久路由；确认投递后再次重开队列不重复发送。
这不证明服务进程重启、实际 IM 传输、真实模型质量或客户端最终呈现。

`tests/test_dispatch_process_restart.py` 另以独立 Python 进程验证正常退出边界：
提交进程持久接单后退出，新的只读 Worker 进程执行，第二个 Worker 进程
不重跑；新交付进程按原路由确认，后续交付进程不重复发送。
模型和 transport 均为替身；这不是 in-flight 强杀恢复、生产服务切换、
整棵后代停止或真实 Gateway/IM 进程验收。

## 候选安装与验证

现有 `candidate-hermes` 构建会应用并记录 `frontdesk-boundary` 补丁；
缺少必需补丁工具或源码接缝漂移会阻止构建继续，不静默省略。
受管发布目录包含补丁和 helper，普通插件安装不改现场宿主。
需要手工检查/应用时，只针对**未运行的候选目录**：

```bash
python3 patches/patch_frontdesk_boundary.py --check --agent-dir /absolute/inactive-candidate
python3 patches/patch_frontdesk_boundary.py --apply --agent-dir /absolute/inactive-candidate
```

补丁先校验所有源码接缝、helper 和转换后语法，再开始写入；
拒绝符号链接源文件/父目录及非普通 helper 目标。
单文件原子替换、helper 先写入，但不是多文件事务或热部署工具。
中途 I/O 失败仍须废弃/修复候选后重新检查，不对运行进程原地应用。

行为回归使用真实宿主模块、临时 HOME、替身执行回调，不发送真实 IM，
并禁止测试子进程打开网络连接或触发自动依赖安装：

```bash
python3 -m pytest tests/test_frontdesk_boundary.py tests/test_frontdesk_turn_release.py \
  tests/test_superagent_installer.py -q
```

`HERMES_SOURCE` 可指定宿主源码；默认使用本机宿主 checkout。
集成用例还从 Git object 提取完整的固定版本
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`，不混用新旧模块。
宿主源码或解释器不可用时相应用例显式跳过，不能计为通过。
`SUPERAGENT_HOST_TEST_PYTHON` 只供测试选择准备好的解释器，不是产品行为开关。

## 尚未完成

候选宿主的强制回合交接已有隔离实现，双身份路由、Worker 执行/交付、
宿主重启和实际 IM 插话纵向验收仍待完成，不能据此称为 P1 完成。
可信研发 containment、停止证明、长期记忆身份/质量、真实 IM 灰度与回退
仍按 `FRONTDESK_DISPATCH_PLAN.md` 和 `RELEASE_READINESS.md` 单独过门。
