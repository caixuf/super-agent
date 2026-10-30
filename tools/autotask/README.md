# autotask —— 把「AI 定时任务」收敛成一个模块

现状问题：每个定时作业都是一段自然语言提示词，**在哪读、写哪、什么时候算成功、失败怎么办**
全写在提示词里，五份模板五种说法；重复作业只能复制粘贴，出错也看不出来。

本模块把一次自动任务拆成**固定的五段契约**，提示词只负责「判断」：

| 段 | 谁决定 | 说明 |
| :--- | :--- | :--- |
| `collect` | 模块 | 任务从哪拿输入（命令 / 文件 glob / 会话记录），只读 |
| `judge` | 提示词 | 唯一需要模型的地方：拿输入做判断/提炼 |
| `write` | 模块 | 结果写到哪（固定的 `state/` 或 `skills/candidates/`），原子写 |
| `report` | 模块 | 成功/失败/跳过三态与判据字符串（`ok <证据>` / `fail <原因>`） |
| `retry` | 模块 | 失败重试上限与退避；超过上限只告警，不再重试 |

## 为什么值得抽象

- **可测**：`collect`/`write`/`report` 都是纯函数，mock 输入即可断言；提示词只剩「判断」这一句；
- **不重造**：调度仍用宿主自己的 cron（Hermes `cron/jobs.json`），本模块只定义作业内容与产物契约；
- **可迁移**：换台机器照抄 `jobs/*.json`，路径与阈值从 `autotask.json` 读，不写死在提示词里。

## 用法（概念）

```jsonc
// jobs/memory-budget.json
{
  "id": "memory-budget-guard",
  "schedule": "0 * * * *",
  "collect": {"kind": "files", "paths": ["<profile>/memories/MEMORY.md", "<profile>/memories/USER.md"]},
  "judge": "超过各自上限 90% 就压缩并留痕；未超限只回一行 ok",
  "write": {"kind": "file", "path": "<profile>/state/autotask/memory-budget.json"},
  "report": {"ok": "^ok ", "fail": "^fail "},
  "retry": {"max": 2, "backoff_seconds": 300}
}
```

渲染器把上面的五段展开成宿主 cron 能执行的一条提示词（`python3 tools/autotask/render.py jobs/xxx.json`），
并把**判据与产物路径**写成模型必须遵守的格式，避免每份作业自由发挥。

## 边界

- 不引入第二套调度器：到期触发仍由宿主 cron 负责；
- 不隐藏动作：`write` 只允许写到 `state/` 与 `skills/candidates/`，业务目录需要显式声明；
- 判断失败要让模型说「不确定」，不许编造成功。
