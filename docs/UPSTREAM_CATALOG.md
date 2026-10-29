# 外部标准与上游模板：哪些直接用，哪些只借格式

`skills/` 与 `identity/` 里的东西都是本仓自己的，但**格式与很多通用做法已有公开标准**。
接线时优先用标准格式，避免自造第二套；业务内容仍然只从业务叠加层来。

## 一、直接采用的标准（不复制、不 vendor）

| 标准 | 用途 | 本仓如何用 |
| :--- | :--- | :--- |
| **Agent Skills / `SKILL.md`**（agentskills.io，Anthropic 公开格式） | 技能 = 一个目录 + `SKILL.md`（YAML frontmatter：`name`、`description`） | 本仓 `skills/*/SKILL.md` 就是这个格式，`tools/install_skills.py` 按目录名 = `name` 校验 |
| **AGENTS.md**（agents.md，60k+ 仓在用） | 给编码代理的项目级说明（构建、测试、约定） | 部署时放在 profile/工作目录根；本仓的 `identity/agents/*.md` 是**派工契约**，与 AGENTS.md 角色不同，不要混用 |
| **JSON Schema**（草稿 2020-12） | 工具参数、卡片结构校验 | `schemas/` 与各插件的工具 schema |

## 二、可以借的现成内容（用前先核对，不盲抄）

| 来源 | 有什么 | 怎么用 |
| :--- | :--- | :--- |
| `anthropics/skills`、`awesome-claude-skills` 等公开技能库 | 通用技能（文档处理、代码审阅、数据转换） | 先看能否直接套用格式；业务相关的不搬，业务词会触发本仓门禁 |
| 各云厂商的插件/扩展清单（如官方 skills 仓） | MCP 服务器配置样例、工具描述模板 | 只借**配置形状**（字段名、连接方式），不把它们的服务地址/密钥带进来 |
| 各平台官方 API 文档 | 卡片 JSON 结构、错误码表 | 一律以官方文档为准；`skills/im-card-markdown` 里的错误码就是官方表的子集 |

## 三、判断口诀

1. **格式用标准**：技能、AGENTS.md、JSON Schema 都有公开定义，不要自造；
2. **内容自己写**：判据、反模式、验收必须来自本仓真实踩过的坑，抄来的内容没人能维护；
3. **凭据与服务地址永远来自部署方**：任何上游示例里的 URL / key 都不进仓。

## 四、升级路径

上游标准变更时（例如 `SKILL.md` frontmatter 加字段）：

- 先改 `tests/test_generic_templates.py` 里的校验；
- 再批量改 `skills/*/SKILL.md`；
- `tools/install_skills.py` 的目录名=name 约定不变，避免安装器跟着改。
