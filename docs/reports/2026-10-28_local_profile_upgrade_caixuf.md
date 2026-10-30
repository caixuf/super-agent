# 2026-10-28 · 本机 caixuf profile 升级记录（对齐 super-agent HEAD）

范围：把本机现役 `caixuf` profile 升到本仓 `master` 的当前提交，并让长驻进程真正加载新代码。
未触碰其它 profile；未改凭据；未发任何平台消息；未改宿主安装树。

## 1. 结论

- 仓与远端一致：本地 `HEAD` 与 `origin/master` 同为 `5b74f11`（`git ls-remote origin` 逐字比对）。
- 资产层已对齐：`install/install.sh --profile caixuf` 退出码 0，托管资产逐字节一致（本轮零写入，
  安装器逐文件 `cmp` 判定后全部走「已一致」分支）。
- 运行态已升级：常驻网关重启完成（PID `165553` → `354716`），新代码进入进程；健康探针、工具面、
  真实工具调用、引擎导入、活体账本真进程冒烟全部通过（见 §5）。
- 三处**不属于安装器序列**的宿主补丁仍未打，属部署决策，本轮按现场既定口径保留（见 §4）。

## 2. 现场快照

| 项 | 升级前 | 升级后 |
| :--- | :--- | :--- |
| 仓提交 | `5b74f11`（= origin/master） | `5b74f11` |
| 网关单元 | `hermes-gateway-caixuf.service` active | active |
| 网关 PID / 启动时间 | `165553` / 2026-10-28 19:29:53 | `354716` / 2026-10-28 21:52:18 |
| profile 资产 | 与仓 HEAD 逐字节一致 | 同左（零写入） |
| 健康探针 | `{"status": "ok", "platform": "hermes-agent", "version": "0.22.5"}` | 同左 |

## 3. 升级动作

```bash
git fetch --verbose origin            # = [up to date] master -> origin/master
git ls-remote origin | head -1        # 5b74f115... == git rev-parse HEAD
bash install/install.sh --profile caixuf --dry-run    # 计划复核：仅 mkdir 幂等项 + 补丁探针
bash install/install.sh --profile caixuf              # 真装：退出码 0，零写入
hermes -p caixuf gateway restart                       # 165553 -> 354716，退出码 0
```

本轮唯一的实际状态变更就是**进程重启**：资产在上一轮部署后已与 HEAD 一致，`install.sh`
的全部步骤都走了「已一致 / 在位」分支。之所以必须重启：插件与 `agentcore` 模块在网关进程内
导入并缓存，文件到位不等于运行态到位（`patches/README.md` 与 `install.sh` ⑫ 都写明了这条口径）。

## 4. 宿主补丁台账（全部逐条实测）

| 补丁 | `--check` 实测 | 现状 |
| :--- | :--- | :--- |
| `apply_feishu_card_replies.py` | `[PASS]` 探针 3/3 在位 | 已应用（安装器序列内） |
| `patch_card_finalize.py` | `[PASS]` 探针 11 处齐全 | 已应用（安装器序列内） |
| `patch_card_markdown_native.py` | `[PASS]` 12 处在位 | 已应用 |
| `patch_feishu_delivery_uuid.py` | `already installed` | 已应用 |
| `patch_durable_dispatch_visibility.py` | `[FAIL]` 不在位（退出码 1） | 未应用：安装器不调用它，T5 报告明确「生产树未改」 |
| `patch_memory_conservation.py` | `needs installation` | 未应用：T7 生产启用未完成，按报告口径走业务仓逐条审阅 |
| `patch_memory_prefetch_observation.py` | `needs installation` | 未应用：同上 |

应用命令（需要时逐条执行；均幂等，改的是宿主安装树，`hermes update` 后须重打）：

```bash
H=$HOME/.hermes/hermes-agent
python3 patches/patch_durable_dispatch_visibility.py --apply --agent-dir "$H"
python3 patches/patch_memory_conservation.py --apply --hermes-root "$H"
python3 patches/patch_memory_prefetch_observation.py --apply --hermes-root "$H"
```

三处补丁都会改宿主安装树或改动记忆写入门禁，属**行为变更**：本轮只记录、不擅自应用。

## 5. 验证证据

### 5.1 工具面（官方口径，非配置文件里的那一行）

```text
$ hermes -p caixuf tools list
Plugin toolsets (cli):
  ✗ disabled  a2a
  ✓ enabled  durable_dispatch  Durable Dispatch
  ✓ enabled  markdown_delivery  Markdown Delivery
  ✓ enabled  task_status  Task Status
```

`hermes plugins list` 侧：14 项用户插件全部 `enabled`（另有两项初始化期插件条目）。
`write_guard`、`longtask_guard`、`memory_bridge`、`illustrated_delivery` 是 hook / 无工具型，
本来就不出现在工具面；`colleague_team`、`colleague_catalog` 的 `settings.enabled` 未给值，
按设计不注册工具（见 §6）。

### 5.2 真实工具调用（走运行中的网关进程）

```text
$ hermes -p caixuf -z "Call the task_status tool ... task_id=smoke-probe-20261028" --yolo --toolsets task_status
RESULT: {"ok": false, "error": "unknown_task", "task_id": "smoke-probe-20261028"}
```

网关进程日志同时留痕（`hermes[354716]` 即新网关）：

```text
21:52:57 WARNING agent.tool_executor: Tool task_status returned error (0.00s):
  {"ok": false, "error": "unknown_task", "task_id": "smoke-probe-20261028"}
```

`durable_status` 同样在真实会话里调用成功（会话范围不允许时拒绝，不伪造结果）：

```text
RESULT: {"ok": false, "error": "task unavailable in this session"}
```

### 5.3 引擎导入（网关解释器 3.14.9）

`card_stream`、`memory_hygiene`、`liveness`、`liveness_daemon`、`pool`、`completion_backfill`、
`markdown_delivery`、`colleague_dev`、`dev_runner`、`colleague_catalog`、`colleague_registry`、
`memory.conservation` 全部导入成功，路径均指向仓内单一源。

### 5.4 活体账本真进程冒烟（临时库，不污染现场库）

```text
t0        state=running pid_alive=True
t1        state=running heartbeat_age_s=0.0 progress={'done': 1, 'total': 3, 'percent': 33.3}
跨会话    {'ok': False, 'error': 'unknown_task'}      # owner 隔离生效
进程消失  state=lost exit_code=None                   # 不是 done
无 pid 静默 sweep 后 state=stalled
```

### 5.5 仓级门禁

```text
$ bash verify.sh
== ① 静态检查（shell 语法 + JSON/YAML 可解析）    [PASS] 9 个 shell 脚本语法正确
== ② 单元测试（python3 -m pytest tests -q）       [PASS] 1234 passed, 20 skipped in 119.60s
== ③ 安装器 dry-run（零副作用）                   [PASS] 退出码 0
== 结果                                          全部通过 [PASS]

$ python3 -m pytest tests/test_repo_neutrality.py tests/test_business_neutrality.py \
    tests/test_import_boundary.py -q
30 passed in 0.75s
```

升级本身对仓是零文件改动（唯一新增文件是本报告），门禁结果与仓既有的 2026-10-28 报告同口径。

## 6. 现场既定事实与边界（非本轮引入、非本轮改动）

1. **模型 provider 断链（预先存在）**：`custom:ccp` → `http://127.0.0.1:4000/v1`（sse_watchdog → litellm 4001）
   对 `/v1/chat/completions` 恒定返回 `400 {"error": {"message": "No connected db."}}`；litellm 侧为
   `user_api_key_auth()` 异常，最早记录在 2026-10-28 00:21（早于本次升级）。profile 内 `providers.ccp.api_key`
   与当前生成的 litellm `general_settings.master_key` 不一致，且 proxy 自身的 db 未连接。
   影响：经该 provider 的模型回合失败（fallback provider 可用，探测回合即走通；失败回合在网关日志可见）。
   本轮未改任何 provider 配置。
2. **同事插件保持 opt-in 关闭**：`colleague_team` 需要宿主私有登记目录与 `team_id/primary_id/registry_path`，
   其 README 明确「不要在现有运行系统上启用并宣称招聘已完成」；`colleague_catalog` 需要管理员角色表。
   两者 `settings.enabled` 不给值即不注册工具 —— 与本轮实测一致。
3. **部署副本残留**：`<HERMES_HOME>/plugins/capability-alert-outbox/capability_alert_outbox/` 是早前布局的
   目录副本（安装器只增不删），当前无引用；未删（属现场文件，未获授权不清理）。
4. **liveness daemon 未接真实 sink**：账本与只读工具已上线；daemon 的传输 sink 仍按报告口径属未完成项。

## 7. 复核命令

```bash
git -C super-agent rev-parse HEAD      # == origin/master
bash install/install.sh --profile caixuf                 # 幂等复核，退出码应为 0
hermes -p caixuf tools list                              # 插件工具面
systemctl --user status hermes-gateway-caixuf.service     # 单元状态
curl -s http://127.0.0.1:8643/health                     # 网关健康
```