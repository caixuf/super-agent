# identity/ — 智能体身份与派工契约（canonical 源）

本目录是**智能体本体**的身份与纪律资产的权威来源（canonical source），不是运行路径。
运行路径在宿主 Hermes 的 home 里。**本目录不含任何业务内容**，业务由部署时注入。

## 文件

| 文件 | 性质 | 业务相关？ |
| :--- | :--- | :--- |
| `SOUL.generic.md` | 身份与纪律的**通用骨架**（工具分层、内部自足、交付纪律、记忆闭环、写保护） | 否 |
| `agents/*.md` | `delegate_task` 的 prose 角色契约（通用骨架，工具名用占位符） | 否 |

业务版的完整身份文件**不在本仓**：它属于业务内容，留在业务仓
（`<business-repo>` 的身份文件）。本仓不保留它的副本。

## SOUL：canonical 骨架 vs 生效副本

- **本目录这份 = canonical 骨架源**（进 git、可评审、可追溯）。
- **生效副本 = `$HERMES_HOME/SOUL.md`**（宿主 Hermes home 下的身份文件；带 profile 时是
  `$HERMES_HOME/profiles/<business-profile>/SOUL.md`，以宿主实际解析的 home 为准）。
  Hermes **每条消息重新读取**它，所以「改哪一份」决定「谁生效」。
- 生效文件 = **通用骨架 + 业务叠加层**：部署时把 `{{BUSINESS_OVERLAY}}` 替换为宿主提供的
  业务内容块（见 `SOUL.generic.md` §8 的注入契约）。骨架里凡需提及业务对象处一律用占位符：
  `<business-domain>` / `<business-tool-name>` / `<business-repo>` / `<business-profile>` /
  `<business-knowledge-base>` / `<business-mcp-server>` / `<business-task-id>` / `<host-identity>`。
- **没有任何东西在自动同步这几份文件。** 这让「仓库里改了但线上没生效」成为一种静默故障
  （契约测试读仓库那份，线上读 profile 那份，**绿测试不代表规则上线**）。因此：
  **只做只读的漂移检测，人工确认后再同步**，不要造自动同步（那会绕开 instruction-file
  写入门禁）。检测口径：对「仓库骨架」与「生效文件」做 whitespace 归一化后的逐字节比对。
- 写保护：受保护的指令类文件写入走它自己的门禁开关（`security.protected_instruction_files`），
  `/yolo`、`HERMES_YOLO_MODE`、`approvals.mode: off` **都绕不过它**；但 **`$HERMES_HOME` 下的
  路径是豁免的**，所以 profile 自己的 `SOUL.md` 不会被拦。

## 门禁（本仓的两个射程）

| 门禁 | 射程 |
| :--- | :--- |
| `tests/test_business_neutrality.py` | `agentcore/` 包内 `*.py` 的**可执行代码**（注释与 docstring 豁免） |
| `tests/test_repo_neutrality.py` | **本仓所有会进版本库的文件**（py / md / yaml / sh / service / template …），只有词表自身所在的那个测试文件豁免 |

所以往 `identity/` 加文件时，它的**正文也在门禁射程内**：举业务例子一律用占位符，不要写真实名字。

## agents/ 的角色契约

`agents/*.md` **不会被框架自动发现**——它们不是按名字加载的 subagent 配置，而是**给指挥官看的
prose 契约**：派工前把对应文件全文内联进 `delegate_task(tasks=[{"goal": ..., "context": ...}])`
的 `context`。所以「部署位」是一个**参考位置**，不是 Hermes 会读的 magic 路径。
详见 `agents/README.md`。