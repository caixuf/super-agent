# IM 个人记忆身份路由

状态：显式启用的候选实现；未迁移生产配置。它覆盖外部记忆的捕获、L1/L2/L3
工具和 localvec 自动召回，不代表整个宿主会话、原生 MEMORY.md、共享 prompt、
工具权限或团队知识已经具备多租户隔离。

## 决策与依赖

宿主插件 → `runtime/hermes_memory_identity.py` → 管理员身份映射。
插件选出明确的 profile/user/agent 和私有目录，再调用既有核心 spool 或独立 CLI。
`agentcore.memory` 不导入宿主、不推断聊天者，也不在一个进程切换全局 MemoryConfig。
每个主体一个独立 runtime_service、数据库、spool、事件队列。禁止不同主体复用或
嵌套状态根；同一主体的多个受信渠道绑定可以指向同一个 runtime。

不选择“session 非空就是主人”，也不从用户工具参数、用户名、模型输出、OS 环境
或原始消息文本推断身份。共享个人库及一个 Worker 扫描多个主体 spool 均不允许。
撤销该决策会涉及三个入口和安装器，因此在此记录共同契约。

## 管理员策略

在新的隔离 profile 外部准备私有 JSON 文件，权限 0600，目录由运行账号拥有。
所有示例 ID 都须替换成宿主传输层实际绑定的稳定 ID，不能从聊天文本认领。
先创建每个 `state_dir` 以及其 `data/`、`spool/` 子目录，权限均 0700。
策略至多 256 条、256 KiB；重复键、未知字段、重复选择器、目录别名冲突、
不安全权限或缺失文件均拒绝。

```json
{
  "version": 1,
  "profile": "frontdesk",
  "profile_home": "/srv/agent/profiles/frontdesk",
  "bindings": [
    {
      "platform": "your-im",
      "scope_id": "workspace-id",
      "user_id": "authenticated-user-id",
      "chat_id": "private-chat-id",
      "principal": "personal-owner",
      "agent_id": "assistant",
      "state_dir": "/srv/agent/memory/personal-owner"
    }
  ]
}
```

必须同时满足：当前 context-local home 对应策略 home；宿主 ContextVars 中的
platform/scope/user/chat 精确匹配；chat_type 为 `dm`；调用 session 与宿主 session
一致；route key 非空；cron 明确为非 cron；不处于委派子执行。复用宿主的
`gateway.session_context._VAR_MAP` 私有接缝，接口缺失即拒绝，升级需重跑宿主契约。
多 profile 模式缺少 context-local home override 也拒绝，不借用启动 profile。
群聊、channel、thread、未知用户、CLI、未绑定/仅环境变量身份默认不能读取或捕获。
团队共享知识需要独立的审核/发布授权链，不把个人库自动共享到群聊。

每次操作重读策略。自动召回按策略摘要、完整渠道选择器、主体及 session 分缓存；
前台返回结果前、后台召回前后、降级 SQLite 查询前后都重新授权。撤销后不返回
旧缓存或在途结果。捕获的 turn key 包含策略代次，映射变化后的终态不能确认旧捕获；
旧记录仍未确认，不自动迁移或消费。已发送的上下文和已准入的旧事实不会被撤销回滚。
同账号可信代码可访问这些文件；这是应用身份路由，不是恶意代码/OS 租户沙箱。

## 准备与安装

准备新 profile 时安装器应显式使用 `--skip-memory`，避免先初始化固定单主体配置。
在新 profile 已启用 `memory-bridge` 后，给策略中的每个 principal 分别运行：

```bash
python install/setup_memory.py \
  --profile-home /srv/agent/profiles/frontdesk \
  --engine-root /srv/agent/releases/candidate \
  --profile frontdesk --user personal-owner --agent assistant \
  --identity-routes-file /srv/agent/profiles/frontdesk/memory-identities.json \
  --embed-url http://127.0.0.1:8910
```

默认只计划；审查后加 `--apply` 才初始化空库、写配置及独立服务文件，不自动启动。
`--activate` 仍要求明确执行、模型/嵌入预检及无未审积压。服务名为
`superagent-memory-<profile>-<principal-hash>.service`，每个主体的环境与队列独立。
配置两侧指向同一份策略，不生成 legacy `user_id`、`recall_cmd` 等固定主体字段：

```yaml
plugins:
  entries:
    memory-bridge:
      settings:
        enabled: true
        engine_root: /srv/agent/releases/candidate
        embed_url: http://127.0.0.1:8910
        identity_routes_file: /srv/agent/profiles/frontdesk/memory-identities.json
memory:
  provider: localvec
  localvec:
    engine_root: /srv/agent/releases/candidate
    embed_url: http://127.0.0.1:8910
    identity_routes_file: /srv/agent/profiles/frontdesk/memory-identities.json
```

缺少 `identity_routes_file` 的旧 profile 保持单主体行为，**不能作为共享 IM 安全配置**。
已配置但值为 null/空/损坏不代表关闭路由；不得回退固定主体或环境召回命令。
宿主报告配置读取失败（含 defaults/last-known-good）或记忆配置段类型损坏时，
provider 不采用环境命令；已经选择路由模式的实例再次初始化也不退回 legacy。
既有单主体配置不能通过此安装命令隐式迁移；需另行明确数据归属、旧 Worker 停止、
积压处置和回退。路由撤销仅取消访问，不负责自动停止旧服务或销毁数据。

受管安装独立链接 `plugins/localvec`，不加入 `plugins.enabled`；doctor 检查链接
属于当前版本，卸载撤除受管链接。仓库 provider 不得被旧外部快照覆盖。
手工复制与其它部署方式仍须核对宿主实际发现的 provider 目录，不能只看源码存在。

管理员可运行只读配置检查（不启动服务、不打开记忆数据库或消费队列）：

```bash
python tools/deployment_readiness.py \
  --profile-home /srv/agent/profiles/frontdesk --profile frontdesk
```

路由模式检查两侧策略/engine/embed 一致、私有主体目录、provider 归属、每个去重
主体的服务状态及保存的队列/主体/环境配置。任一主体缺失、unit 漂移或旧单主体
服务仍活跃/状态无法核实时均保留 OPEN，不自动修复或停止服务。配置重复键、
类型损坏、策略无效不回退 legacy。它不读取 systemd 已加载配置或进程环境，
不能证明服务进程实际使用保存文件、真实身份传输或模型质量；CONFIGURED 不是
实际运行验收。其它尚未实现的整体现有门禁仍使命令返回非零。

## 验证与未完成项

`tests/test_memory_identity_routes.py` 覆盖未知身份读取的红绿反例、双用户并行读取/
捕获、群聊/会话/profile/委派拒绝、环境身份不能冒充、损坏/重复/共享路径策略、
撤销缓存及在途结果、总召回容量、双 Worker 安装与拒绝隐式迁移。
另从当前及固定候选宿主完整源码加载实际 ContextVars、provider 发现和自动召回，
只使用临时 HOME 和离线数据。没有宿主源码的干净室仍执行基础契约，只跳过两项
明确的实际宿主集成；这些测试不发送真实消息或调用真实模型。
`tests/test_deployment_readiness.py` 另外覆盖双主体 unit、同主体多渠道去重、
单主体失败/漂移、旧服务冲突、查询异常及配置故障关闭；数据库使用不可解析
标记文件，确保就绪检查没有依赖读取记忆正文。

仍需真实身份/IM 回读与并发验收、宿主共享上下文隔离审计、固定样本准入/召回质量
评测、团队审核发布、授权的生产切换和回退验证。测试通过不解除这些发布门禁。
