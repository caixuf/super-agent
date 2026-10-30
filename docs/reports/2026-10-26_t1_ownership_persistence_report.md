# T-A 任务归属持久化（手册 G3）· 测试报告

日期：2026-10-26
执行人：Agent（远端宿主 现场，`agentcore`）
上游依据：`<platform-root>/docs/handover/2026-10-26_t1-wiring-field-map.md` 缺口 G3
结论：**代码完成、离线全验通过、现场库迁移已在真实副本上验证**；生产进程尚未加载（需窗口重启），因此**不构成 T1 完成**。

## 一、问题

手册要求「任务归属必须持久化：路由只认提交时保存的 owner/profile/session/thread/route，
绝不使用『当前活跃会话』推断回填目标」。

改之前：`plugins/durable_dispatch` 只把 `_dispatch_owner = sha256([profile, session_id])`
塞进 `jobs.payload`。这个哈希**能验证归属，不能寻址投递**——拿它算不回 profile/session，
更算不回 chat/thread，所以后台完成事件在网关重启、用户切窗口之后无法确定回填目标。

## 二、改动（4 个文件，均显式路径）

| 文件 | 改动 |
| :--- | :--- |
| `agentcore/pool.py` | `jobs` 表新增 `origin TEXT`（幂等 `ALTER TABLE`，与 `pid/pid_start/deadline` 同一迁移块）；新增 `normalize_origin()`；`submit(..., origin=None)`；新增 `origin(job)` 读取接口 |
| `plugins/durable_dispatch/__init__.py` | `submit` 时持久化 `origin={'profile': self.profile, 'session': session_id}` |
| `tests/test_pool.py` | 新增 5 组用例：归属落盘/去重身份含归属/字段校验 7 组参数化/旧库迁移 |
| `tests/test_durable_dispatch.py` | 新增 2 组用例：提交记录提交方会话/每任务归属可区分且不放宽越权校验 |

设计取舍（写进代码注释，避免下一个人重推）：

- `origin` **只存提交方真正持有的身份**：`profile` + `session` 必填，`route/channel/chat/thread/turn`
  有则记、缺则缺，**不猜**。空白字符串一律拒绝（空的 route 看着可寻址却路由不到）。
- 未带归属的旧任务读回 `None`，不是 `{}`：显式表达「这条历史任务不可寻址」。
- 归属纳入去重身份比较：同一 `request_key` 换会话重试不再静默复用旧行（防止把 A 会话的任务
  继承给 B 会话的回填目标）。
- `_dispatch_owner` 保留不动——它是**归属证明**（单向），`origin` 是**地址**，两者职责不同。

## 三、命令与结果（原样）

```
$ cd agentcore
$ TMPDIR=/dev/shm python3 -m pytest tests/test_pool.py tests/test_durable_dispatch.py -q
36 passed in 3.95s

$ TMPDIR=/dev/shm python3 -m pytest tests/ -q
357 passed, 9 skipped in 33.18s
```

9 项 skip 均为既有可选依赖缺失（`flowcoro_py` 8 项 + native binding 1 项），与本次改动无关。

## 四、生产库迁移验证（在真实 `pool.db` 副本上做，只读源库）

```
源：~/.local/state/agentcore/analysis-queue/pool.db  →  复制到 /dev/shm 后操作
before_cols: [..., 'pid', 'pid_start', 'deadline']
after_cols : [..., 'pid', 'pid_start', 'deadline', 'origin']
rows before/after: 3 3
legacy origin read: [None, None, None]
new job origin: {'profile': 'p', 'session': 's'}
rowcount now: True
```

含义：迁移是**纯加列**，现有 3 条任务行一条不少，旧行读作「不可寻址」，新行带归属。
旧进程（当前 worker）继续用显式列 INSERT 不受影响；它写入的行 `origin` 为 NULL。

## 五、顺带修掉的一条既有红灯（不属于本任务，但同一次跑测暴露）

`tests/test_repo_neutrality.py::test_no_business_literals_anywhere_in_repo` 之前是红的，
来自 T3 合成数据 `tests/fixtures/memory_recall_cases.json` 里的 revision 标签命中了禁用词表
（该表把某两个「车型代际代号」列为禁用）。那条用例的语义是「旧 revision 的事实不应被当作
当前事实」，与具体业务无关，与代际代号只是字面重合。已把 revision 标签改成
`rev-a` / `rev-b` / `rev-0` 这类中性写法，事实 id 同步改名，语义不变。
**该文件仍由 T3 线持有，本次不提交。**

（本报告初版描述这段时直接抄了那几个字面量，被同一道门禁在自查时抓红——说明门禁覆盖含 `docs/`，
记录问题时同样要不写实名字面量。）

## 六、未验 / 边界（不要当成已完成）

1. **生产进程未加载**：网关进程内的插件是 13:23 启动时载入的旧代码。闸门要求「网关空闲 + 修改
   全审阅 + 备份 + 回滚命令就绪」才允许 `systemctl --user reload`（实为优雅重启），本次未做。
   所以现在生产提交的任务仍然**不带 origin**。
2. **route/chat/thread 仍未落库**：宿主工具面只给 handler `session_id`（`agent/tool_executor.py`
   里 `session_id=...` 是唯一身份），`ctx` 也不暴露 `SessionSource`。要存完整 route 需要宿主
   暴露 source，属于 G2/G4 那批补丁的范围，本任务没做。
   因此「按归属寻址」当前只能到 `(profile, session)` 一级，route 仍需查 `sessions` 表补齐。
3. **没有投递侧消费者**：`origin` 目前只解决「谁提交的」，不解决「谁去投递」。后台完成事件
   仍然走 `_inject_watch_notification → admit_automation`（G2，未做）。
4. 未做多进程并发提交压力测试；只覆盖了单进程与既有并发去重用例。

## 七、回滚

`git revert` 本次 commit 即可。生产 `pool.db` 多出的 `origin` 列是加列，回滚代码后不会被读取；
**不要**为了回滚去 `DROP COLUMN`（WAL 下重写全表，收益为零）。旧行为与 NULL 列完全兼容。

## 八、下一步（只写一个）

在窗口内重启网关（先备份 `pool.db` 与 profile 配置），用一次真实 `durable_submit` 确认
`jobs.origin` 落库，再进入 G2 的投递侧改造。
