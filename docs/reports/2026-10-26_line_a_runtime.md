# A线网关与派工运行时交接

日期：2026-10-26。此报告记录A线证据和隔离补丁；生产验收仍由root与用户完成。

## T1：前台保护与忙时消息

- `longtask-guard`的宽泛`timeout > 30`触发已从分类器移除。`timeout/env ... pytest`仍依据实际执行命令命中重型测试规则；`timeout`数值单独不足以把未知命令判为重活。
- 实际形态`sed -n`、有界`awk`、`for ... printf`且terminal timeout=120的回归为负例。专项回归36项通过。root随后完整回归284项通过，部署guard 1.0.2，并现场核对`short_query_background=false`、`heavy=true`、gateway PID 3381565未变。这里没有真实用户请求链路验收。
- 忙时配置当前为`display.busy_input_mode: steer`。宿主在运行中的Gateway启动时读取此值；改成`queue`后需通过受控reload/restart创建新快照。现有`_queue_or_replace_pending_event`与`_enqueue_fifo/_promote_queued_event`保留每个独立消息的FIFO turn，照片burst仍按现有相册语义合并。相关宿主回归包括`tests/hermes_cli/test_cli_queue_gateway_fifo.py`和`tests/gateway/test_queued_followup_processing_hooks.py`；本轮未修改、未运行这些宿主测试，也未改生产配置。
- `queue`模式可让正在drain时到达的消息进入已有队列；应在root选定的空闲窗口实施，确认待处理turn已收尾，记录旧配置和服务PID，切换配置并优雅reload，核对新PID/新启动快照，然后由用户在服务恢复后手发三种并发场景。失败时恢复`steer`并reload。T5若在同一窗口启用，还需按T5 runbook同时处理`display.platforms.feishu.streaming`和`platforms.feishu.card_replies`，维护者应避开灌灌API流量；reload drain期间最多75秒，不要把生产窗口当单元测试。
- 通知风暴源于多条短查询分别启动不同process、各自完成通知；不是同一个进程通知重复。宿主已有`read_log`完成消费标记和watcher/drain检查。`poll`只标观察而不抑制自动通知是既有契约；本轮未改通知去重。

## T5：同一卡与真终态

- T5补丁`ae5edba`及宿主侧11处幂等补丁已在独立进程通过23项真实OpenAPI验证。生产`card_replies`与`streaming`仍关闭，Gateway未因T5重启。此状态不能报告为生产开关验收。
- 维护者要求的`already_sent=True`不删流式终稿、`already_sent=False`清理多余预览对照已补入`patches/verify_card_finalize.py`，不改宿主文件和既有T5补丁。当前执行verifier仍未成功：尝试的Hermes Python命令在载入Gateway导入时报告缺少`ruamel.yaml`，虽然相同解释器单独导入该包及`gateway.stream_consumer`成功；需由root用已验证的host测试启动方式复跑，再判断此契约补充是否通过。
- 宿主树还有`gateway/run_turn.py`、`gateway/stream_consumer.py`、`gateway/stream_consumer_transport.py`、`plugins/platforms/feishu/adapter.py`、`tools/session_search_tool.py`等共享未提交改动。T5验证需确认预览清理只删独立过期预览，不删`already_sent`同卡终稿；不得覆盖这些diff。

## T2：持久分析入口可见性草稿

- 只读取证显示：profile启用`durable-dispatch`，`platform_toolsets.feishu`含`durable_dispatch`；插件通过可信session/profile身份与管理员allowed roots执行owner隔离。Hermes没有plugin manifest或`register_tool`的公开direct/eager opt-in；tool-search默认把插件工具延迟到bridge。
- 14:52:24长期Feishu会话日志记录tier 1、15个直显工具、54个deferred、catalog listing=`full`，之后只记录`delegate_task`，没有`tool_search/tool_describe/tool_call/durable_submit`调用。delegate_task工具契约明确适合重推理/独立子任务，profile的`model_map.fast`角色也明确适合搜索、读日志与简单核验。fresh API会话实际调用过`durable_submit`。这说明不是插件/Feishu toolset缺失；最可能是临时委派更直接、更符合旧角色契约。旧会话schemas的session pin只留有hash，不能从当前只读DB恢复原始tool数组，故不能断言模型当时精确看见了哪个schema。
- 草稿改动：`plugins/durable_dispatch/__init__.py`注册bounded routing prompt section；`patches/patch_durable_dispatch_visibility.py`与对应verifier仅把`durable_dispatch`单一toolset加到Hermes direct-surface allowlist；`tests/test_durable_dispatch_visibility.py`在临时Hermes副本中跑真实registry/adapter。测试证明启用时三工具直显、禁用时缺席，其他插件不会因此直显。`tests/test_durable_dispatch.py`更新fake context契约。两测试文件合计20项通过（含owner隔离、真实注册、工具可见性、prompt注册），运行在`/dev/shm`。
- patch脚本未应用到宿主安装树，插件prompt也未热加载。schema pin和系统prompt在既有会话中冻结；需新session/明确工具重建后才可验证生效。
- Hermes `register_tool`没有官方“提交后强制结束主回合”的effect/end-turn/handoff参数。`durable_submit`只返回文字建议“Return the task ID ... finish this turn”，因此direct visibility和角色prompt降低选用摩擦，但无法强制模型终轮、无法阻止后续等待或poll。测试证明插件submit返回queued/task_id、不在主线程等worker；自然语言路由及主回合立即结束仍须真实验收。

## 文件与验证

本阶段只改了以下A线文件：

- `plugins/durable_dispatch/__init__.py`
- `tests/test_durable_dispatch.py`
- `tests/test_durable_dispatch_visibility.py`
- `patches/patch_durable_dispatch_visibility.py`
- `patches/verify_durable_dispatch_visibility.py`
- `patches/verify_card_finalize.py`（只加离线预览清理反例与对照）

`git diff --check`干净。T2测试：20 passed；T1专项：36 passed；T5新加的verifier对照尚未确认通过。未改配置、未热加载/重启生产、未发外部消息、未调用模型、未commit/push。