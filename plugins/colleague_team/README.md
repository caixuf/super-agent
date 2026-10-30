# 团队管理工具

这个插件提供岗位登记、招聘、授权和停用申请。主同事可以授权一位成员管理团队；获得授权的成员也可以招聘和授权其他成员。调用者身份由宿主的 profile 决定，模型不能在工具参数里指定自己是谁。

主同事身份在登记库中固定。任何团队成员都不能通过这些工具停用或删除主同事。普通成员可以查看团队和岗位，但不能修改团队。

工具默认关闭。启用前由宿主准备私有登记目录，并为各成员配置同一个团队 ID、主同事 ID 和登记库路径。成员的 profile 名称需要与其成员 ID 一致。岗位职责和边界是描述，不会自行授予文件、工具或凭据访问权。

## 工具

- `colleague_list`：分页查看成员和岗位；运行状态来自宿主最近的观察。
- `colleague_define_role`：定义岗位名称、职责和工作边界。
- `colleague_hire`：登记一位成员，交由宿主创建实例。
- `colleague_grant`：给成员授予或撤销团队管理权。
- `colleague_retire`：申请停用成员。宿主应先阻止新任务、排空已有任务，再停放实例；历史记录保留。

每次修改需要稳定的 `request_id`。同一个动作因连接中断而重试时使用原 ID；有意发起新决定时使用新 ID。重复请求返回的是当时的操作回执，要查当前状态请重新调用 `colleague_list`。

## 当前边界

登记成功只是记账，不能说明实例已启动。此插件尚未接上真实的实例创建驱动、岗位派工、最终消息发送和生产停用流程；不要在现有运行系统中启用并宣称招聘已完成。

主同事保护适用于本登记库及其管理工具。要防止拥有同一系统账号的任意终端命令直接删除主同事文件，还需要宿主进程隔离和文件权限；登记库本身不能提供这项保证。

## Gateway 派工（额外关闭）

`gateway_dispatch.enabled` 默认关闭。即使打开，插件也只注册工具外壳；没有宿主安装的可信 dispatcher 时，调用会返回 `gateway_dispatch_host_unavailable`。工具只接受同事 ID/唯一显示名、任务文本和幂等 `request_id`。profile 与 session 从 Hermes 当前 runtime 和 handler 注入的 `session_id` 绑定，模型不能传 owner、generation、profile、principal、Gateway 地址或 session ID。状态查询只能从提交任务的同一 profile/session 读取。

真实宿主必须先构造并验证 `GatewayColleagueDispatcher`（Registry + Supervisor、真实 Provisioner、持久 Hermes session binding store、按 profile 绑定的 `HermesGatewayHost.connection_for_profile`），再在插件工具可用前显式注册：

```python
from agentcore.colleague_gateway_service import register_gateway_dispatch_service
register_gateway_dispatch_service("leader", dispatcher)
```

宿主也可使用 `agentcore.colleague_gateway_bootstrap.ColleagueGatewayBootstrap` 组装和注册这些组件，并在退出时关闭。它需要宿主传入真实后端和成员配置；本身不会启动 Gateway 或创建成员。

`agentcore` 包不 import 宿主运行时：建连与会话存储都由宿主注入。本插件附带 `plugins/colleague_team/host_runtime.py` 提供真实实现，归宿主侧使用：

```python
from plugins.colleague_team import host_runtime
host = HermesGatewayHost(profiles, connector=host_runtime.connect_profile)
bootstrap = ColleagueGatewayBootstrap(..., gateway_host=host,
                                      session_store_factory=host_runtime.session_store)
```

没注入时 `connection_for_profile` 报 `gateway_connector_required`、会话创建报 `session_store_factory_required`，不会退回任何隐式 import。插件启用派工时会同时调用 `colleague_gateway_service.configure_session_env(get_session_env)`；没有这个注入，派工工具拒绝运行（`session_env_provider_required`），因为无法判断当前是否 cron 会话。

宿主必须自己提供 readiness attestation；仅能连上 Gateway 不代表成员 ready。当前仓库还没有可供生产直接使用的 Provisioner backend，也没有在现有服务中启动 bootstrap，因此工具仍不可用，不能通过设置开关伪装完成接入。Gateway 回执 terminal 不会自动关闭 reservation；后台进程未有宿主 quiescence 证据时保持占用。插件不发送 Feishu 消息。

查状态时会附带一条简短说明：排队、处理中、状态待确认，以及「同事已回复但后台工作仍待确认」分别显示。只有答复已确认且后台占用已关闭，才说任务完成。

较长答复按最多 12000 个字符分段返回。结果中的 `response_next_offset` 非空时，在同一会话用相同任务号再次调用 `colleague_task_status`，把它作为 `response_offset`，并传回 `response_revision`，直到 `response_next_offset` 为空。每页都重新核验 Gateway 的会话身份和答复版本；若答复已变化，需从第一页重新读取，避免拼接不同版本。消息历史、推理过程和其他会话的答复不会随分页返回。此功能需要上述 Gateway 派工接线，并不代表实际成员已可用。
