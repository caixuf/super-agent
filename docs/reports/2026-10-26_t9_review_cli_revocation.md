# T9 审查 CLI + 撤销流程（2026-10-26）

## 状态

完成。新增审查 CLI 与候选撤销状态；全部离线测试通过，未提交、未触碰生产数据。

## 改动清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `agentcore/learning/review_cli.py` | 新增 | `list` / `show` / `approve` / `reject` / `revoke` 五个子命令；退出码常量化；`main()` 可注入 stdout/stderr |
| `tests/test_learning_review_cli.py` | 新增 | 17 个离线用例，全部使用 `tmp_path` 候选库 |
| `agentcore/learning/candidates.py` | 最小增量（允许范围内） | `STATUS_REVOKED`/`DECISION_REVOKE` 常量、进入 `STATUSES` 与 `TERMINAL_STATUSES`、新增 `CandidateStore.revoke()`；文档补充撤销规则 |
| `agentcore/learning/__init__.py` | 附加导出 | `STATUS_REVOKED`、`DECISION_REVOKE` |

未修改 `promotion.py`。`promote_candidate` 本身只接受 `approved`，因此 `revoked`
候选会在解析记忆库之前被 `CandidateNotPromotable` 拒绝（有专门用例证明假记忆库的
`write_fact` 零调用）。

## 撤销语义

- 只有 `approved` 候选可撤销；`pending` 请用 `reject`，`rejected`/`revoked`
  重复撤销一律拒绝。
- 撤销是终态（`TERMINAL_STATUSES`），写入 `review_status=revoked`、`reviewer`、
  `reviewed_at`、`review_note=reason`，并在 `reviews.jsonl` 追加
  `decision=revoke / outcome=revoked / note=reason` 审计行。
- 审查者由存储层校验：非空、非自动化身份（`auto/system/...`）、非候选作者。
- `revoked` 不参与冲突判定（`_CONFLICTING_STATUSES` 未包含），撤销后不再阻塞
  其他候选审批。
- 状态可持久化：重开 `CandidateStore` 后 `revoked` 正常加载（`from_dict` 用
  `STATUSES` 校验）。

## CLI 面与退出码

```text
python -m agentcore.learning.review_cli --root DIR list [--status S] [--domain D]
python -m agentcore.learning.review_cli --root DIR show <id>
python -m agentcore.learning.review_cli --root DIR approve <id> --reviewer NAME [--reason TEXT]
python -m agentcore.learning.review_cli --root DIR reject  <id> --reviewer NAME --reason TEXT
python -m agentcore.learning.review_cli --root DIR revoke  <id> --reviewer NAME --reason TEXT
```

`--root` / `--json` 可放在子命令前或后；`--root` 必填，无默认路径。

| 退出码 | 含义 | 触发示例 |
|---|---|---|
| 0 | 成功（含空列表） | `list`、批准/否决/撤销成功 |
| 1 | 业务拒绝 | 未知候选、`ReviewBlocked`（冲突）、作者自审、自动化审查者、空白 reason、终态重复操作、只允许 approved 的撤销 |
| 2 | 用法错误 | 缺 `--root`、缺必填 `--reason`/`--reviewer`、未知子命令/状态 |
| 3 | 存储读写失败 | 候选库目录无法打开或 IO 错误 |

输出：文本模式 `list` 为 `ID\tSTATUS\tDOMAIN\tREVIEWER\tCLAIM` 制表行；
`show` 为键值行；动作命令为 `STATUS ID reviewer=NAME at=TS`。`--json` 时
`list` 输出数组、`show` 输出候选字典、动作输出 `{command, candidate}`。

## 重复/幂等行为（显式拒绝，不静默成功）

- `approve` 已批准：退出 1，`已终结于 approved，不能再次审查`，状态/审计不变。
- `reject` 已否决：退出 1，`已终结于 rejected...`。
- `revoke` 已撤销：退出 1，`已终结于 revoked，不能再次撤销`。
- 重复动作不会新增 `reviews.jsonl` 行；撤销后 `approve` 同样退出 1。
- 撤销不是幂等“成功”：重复调用必须显式失败，避免掩盖状态漂移。

## 验证证据

```bash
# 新增切片
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_learning_review_cli.py -q
# 17 passed

# 既有学习层回归
TMPDIR=/dev/shm python3 -m pytest \
  tests/test_learning_candidates.py tests/test_learning_promotion.py \
  tests/test_learning_retrieval.py tests/test_learning_http_fetcher.py -q
# 136 passed

# 全仓库
TMPDIR=/dev/shm python3 -m pytest tests -q -p no:cacheprovider
# 692 passed, 9 skipped（跳过项为可选原生绑定 flowcoro_py）
```

手工 smoke（临时 `/dev/shm` 候选库）：`show` 0；作者自审 1；`approve` 0；
重复 `approve` 1；`revoke` 0；重复 `revoke` 1；`list --status revoked` 0；
缺 `--root` 2。输出与退出码符合上表。

## 剩余工作

1. **认证/角色**：CLI 目前只要求具名 reviewer，不校验身份真实性、角色或审批
   权限；生产接入需外部身份（token/SSO）与角色表。
2. **UI**：无交互式审查界面；CLI 输出为纯文本/JSON，未做分页、筛选保存或批量
   操作。
3. **事实撤销**：本次只撤销候选状态，不会从正式记忆库撤回已晋升的 fact；
   需要 `write_fact` 侧的删除/失效接口与 provenance 反查。
4. 审查通知、撤销影响面提示（已晋升/召回探针状态）未实现。

## 生产边界

- 所有测试与 smoke 均使用 `tmp_path` / `/dev/shm` 临时目录，未写入任何生产
  候选库或正式记忆库。
- 未提交 git；仓库内存在他人未提交改动（README、document_intent、
  patches 等），本次未触碰。
