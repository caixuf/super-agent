# 通用定时作业模板

本目录只放**与业务无关**的作业模板。业务作业（跑测、需求巡检等）由业务叠加层给，不进本仓。

## 怎么用

1. 把需要的模板复制进 `<profile>/cron/jobs.json`（该文件在 git 之外，安装器不覆盖）；
2. 改 `name` / `schedule` / 提示词里的占位符（`<profile>`、`<business-domain>`）；
3. 用 `hermes --profile <p> cron list` 确认已登记，`cron run <name>` 先手动跑一次看输出。

模板里所有作业都遵守两条纪律：**只读优先**（不改代码、不重启服务）、**输出可判定**（成功要有明确判据，
失败要么重试要么告警，不许静默）。

| 文件 | 作业 | 频率 | 作用 |
| :--- | :--- | :--- | :--- |
| `memory-budget.json` | 记忆预算守护 | 每小时 | 记忆文件接近容量上限时压缩并留痕，避免后续写入全部失败 |
| `capability-drift.json` | 能力漂移巡检 | 每天 | 对比基线（工具面/插件启用）与现场，发现被关掉的工具 |
| `session-archive.json` | 会话归档 | 每天 | 把长会话压成摘要清单，控制上下文增长 |
| `job-retry-watch.json` | 失败作业巡检 | 每 2 小时 | 找出连续失败的作业并告警，避免「跑了但没人知道挂了」 |
| `skill-distill-candidate.json` | 技能候选提炼 | 每天 04:20 | 按 `skills/skill-distillation` 的门槛从昨日任务里产出 ≤3 条候选，写到 `<profile>/skills/candidates/<日期>.md`，不直接进 `skills/` |

## 候选怎么转正

1. 先按 `skills/skill-distillation` 的评审四条过一遍（判据可执行、反模式有真实代价、脱敏、名字能看出何时用）；
2. 把它挪到 `skills/<name>/SKILL.md`（技术仓）或业务叠加层技能源（业务内容）；
3. 跑 `python3 -m pytest tests/test_generic_templates.py -q` —— frontmatter、`name`、`Use when` 三条会立即校验。

## 夜间会话复盘模板

nightly-self-review.json 是不绑定机器或业务 profile 的模板；安装器默认只检查，不创建。先确认目标 profile、工作目录和时区，再显式使用 --install：

    python3 tools/install_nightly_review.py --profile PROFILE --profile-home /absolute/profile/path --workdir /absolute/repository/path --timezone Asia/Shanghai
    python3 tools/install_nightly_review.py --profile PROFILE --profile-home /absolute/profile/path --workdir /absolute/repository/path --timezone Asia/Shanghai --install

安装器只通过 Hermes cron create 添加作业；它不改全局时区，不覆写已有作业。它要求 profile 配置里有明确 timezone 且与参数相同。Hermes cron 没有单作业时区：当前 caixuf profile 的 timezone 查询为空，因此该 profile 会被拒绝安装，不能据服务器当前偏移推断配置。即使 profile 配置明确，运行中 Gateway 的 HERMES_TIMEZONE 服务环境仍可能覆盖它；成功状态会标为 ADDED_PENDING_RUNTIME_TZ_CHECK，需由部署者核对活动 service 环境后再视为确认。已知旧作业名“夜间会话复盘与改进”会返回 LEGACY_CONFLICT，须人工检查/迁移，安装器不会另建重复作业。
