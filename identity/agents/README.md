# agents/ —— 派工契约，不是 Hermes profile

这些 `.md` 不是按名字加载的 subagent 配置，也不会被框架自动发现。
它们是**给指挥官看的 prose 契约**：派工前把对应文件全文内联进
`delegate_task(tasks=[{"goal": ..., "context": ...}])` 的 `context`。

## 事实（Hermes v0.22.1）

- `delegate_task` 只接受 `tasks` 列表，每项 `{goal, context}`（可选 `output_schema`）。
- **没有 `agent_id`**；worker 名字只存在于契约文本里。
- **没有 model-facing `toolsets` 参数**；MCP 工具面由配置模板
  （`{{BUSINESS_OVERLAY}}`）里的 `delegation.inherit_mcp_toolsets: true` 继承，
  可用工具实际来自 `<business-mcp-server>`。
- worker 是叶子：`max_spawn_depth: 1`，不能再派孙子；需要串联时由指挥官按阶段依次派，
  前一阶段回执作为后一阶段 context 的输入。
- 不要为每个角色新建独立 Hermes profile。standalone gateway 只服务
  `<host-identity>` 一路（即 `<business-profile>`），多 profile 需要额外端口与复用配置，
  收益不抵成本。

## 派工写法

```python
delegate_task(tasks=[{
    "goal": "对任务 <business-task-id> 的失败做定性，输出 product_bug / case_error / requirement_change / undetermined",
    "context": """你是 triager（归因诊断专家），只做以下职责：

<把 agents/triager.md 全文粘到这里>

输入：
- business-task-id: <business-task-id>
- 失败用例路径: <business-repo> 里的相对路径
- 用户问题: ...

必须按契约返回回执字段；证据不足写 undetermined，不要编推理。""",
}])
```

要点：

1. `goal` 只写一件可被回执验证的事，不要写"帮我看看"。
2. `context` 内联契约全文 + 输入路径 + 期望回执字段；worker 看不到本仓库，也看不到
   你脑子里的背景。
3. 独立的只读查证可以一次并行派多个 task；有前后依赖的必须串行。

## 角色清单

| 文件 | 角色 | 产出 |
| :--- | :--- | :--- |
| `reader.md` | 只读查证 | `found` / `missing` / `candidates` / `is_degraded` |
| `triage.md` | 变更分诊师 | `affected_cases` / `affected_units` / `scope_confidence` |
| `author.md` | 用例作者 / 门禁守卫 | `case_path` / `is_degraded` / `gaps` |
| `runner.md` | 仿真仲裁员 | `task_id` / `status` / `repeat` / `is_degraded` |
| `triager.md` | 归因诊断师 | `verdict` / `evidence` / `first_divergence` / `is_degraded` |
| `closure.md` | 交付与演化员 | `evolved` / `skipped` / `tool_gaps` / `recall_verified` |

## 回执纪律

- `is_degraded: true` → 没上真环境，不能当真实结果报。
- `scope_confidence: low`、`verdict: undetermined`、`recall_verified: false` 都是
  worker 在诚实地说"没查全"，指挥官必须照实转述缺口，不能包装成确定结论。
- 没有证据就写 `ok: false` / `missing` / `candidates`，不要用报告摘要或推测填满字段。