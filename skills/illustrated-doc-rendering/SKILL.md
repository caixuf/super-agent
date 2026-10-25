---
name: illustrated-doc-rendering
description: "Use when 要把解释/架构/机制内容渲染成带图文档交付（自包含 HTML + 内联 PNG）。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [documentation, mermaid, html, screenshot, deliverables, messaging-delivery, diagrams]
---

# 把解释渲染成带图文档（Illustrated Doc Rendering）

**用这个 skill 回答"架构/机制/多分支解释"这类问题时。** 这类内容在 IM/工单里必然被降级
（表格被渲染成源码、图根本没有），所以**出口只能是文档**：一张内联的 PNG + 一份可点开的自包含 HTML。
纯文本不是"保守选择"，是把可读性主动丢掉。

## When to Use

判据（满足其一就出文档，不要问用户要不要）：
- 内容里有 **≥2 个角色/分支/阶段的流转**（谁调用谁、谁在什么时候改状态）；
- 内容里有 **表格**（IM 里会退化成源码）；
- 回答长度预计超过一屏，或用户会拿去转发给别人。

反过来，**别滥用**：一句话结论、单一事实查询、命令行输出，直接文字回答更省事。

## 交付形态（默认两件，一起给）

| 产物 | 给谁看 | 为什么需要 |
| :--- | :--- | :--- |
| `<名>.png` | 内联进消息 | 对方**零环境**即可读，转发不丢格式 |
| `<名>.html` | 人/归档 | 自包含（内联 CSS + 语法高亮 + 本地 mermaid），双击可读、锚点可跳 |

不要同时把 Markdown 源码再贴一遍——那是第三份副本，只会稀释结论。

## 程序（按顺序，别跳）

1. **写 Markdown 源**：`# 标题` + 紧跟一行 `> 一句话摘要`（会被封面当 lede 吃掉）+ `## 小节`。
   图用 ` ```mermaid ` 围栏。写作规范见 `references/mermaid-authoring.md`。
2. **渲染**（工具在智能体本体仓，通用能力，别拷到业务仓）：

   ```bash
   python3 tools/arch_doc.py doc.md -o out/ --meta 版本=v2 --meta 变更=xxx
   python3 tools/arch_doc.py doc.md --png-only     # 只要图
   python3 tools/arch_doc.py doc.md --width 1100   # 图要更大就收窄页宽
   ```

   依赖：python-markdown / pygments / Pillow + chromium 或 google-chrome（**不需要 node**）。
   mermaid 首次运行自动拉取到 `tools/assets/`（该文件在 `.gitignore` 里，别提交）。
3. **机械验证**（`scripts/verify_render.sh <html> <mermaid 块数>`）—— 两条**互补**，都要过：
   （a）`--dump-dom` 输出里 `data-processed="true"` 的个数必须**等于** mermaid 块数（这条只在 DOM 里可见）；
   （b）在**静态 HTML 文件**上 `grep -o 'class="language-mermaid"' | wc -l` 必须为 **0**（这条不能在 `--dump-dom` 上查）。
   "文件生成了"不是证据——图可能根本没渲染。
4. **自己看一遍图**（vision 能力看图）：把 PNG 当校样读，只认**真缺陷**，逐条改掉再交付。
   反复被读出来的类别：星号/反引号外露、标题三连重复、绝对路径泄漏、代码块无高亮、
   图分色为零、多线汇聚成一点被读成"A 到 B"、目录不像链接。
5. **交付**：PNG 内联 + HTML 附件，配一句「结论先行 + 未覆盖项」。

## 坑（每条都是机制，不是偏好）

- **mermaid 围栏要连围栏一起换掉。** 只把开围栏改成 `<div class="mermaid">` 而保留 ` ``` `，
  markdown 依旧按代码块渲染：图上屏是**源码文字**，DOM 里 `data-processed` 为 0。
- **截图必须给 `--virtual-time-budget`。** mermaid 是异步渲染，不给虚拟时间预算就截到还没画完的页面。
- **mermaid.js 必须内联本地，不能挂 CDN。** 截图正确性不能依赖出网：CDN 断则 PNG 里只剩空框。
- **Pygments 的 token 样式必须内联。** 只给 `class="codehilite"` 而不内联样式表，等于**没有语法高亮**
  —— 文档里却写着"语法高亮"，名实不符最快被看出来。
- **封面要吃掉正文的 H1 与首段引用。** 否则同一句话在封面、目录、正文里出现三次。
- **封面的摘要要做行内渲染**（先转义再还原 `**加粗**` / `` `代码` ``）：直接转义会把星号原样吐出来。
- **目录用渲染器自己生成的 toc。** 自己按规则拼锚点，与正文生成的 `id` 对不上，点过去就是死链；
  测试要断言"目录里每个 `href` 都能在正文找到同名 `id`"。
- **页脚默认只写文件名，不写绝对路径。** 文档是要外发的，绝对路径里带用户名和内部目录结构。
- **裁掉底部留白用 `ImageChops.invert(im).getbbox()`**，别逐像素判白：既慢又会被类型检查噪音缠住。
- **判定"图有没有渲成"时有三个假阳性，实测连踩三次**，口径必须写死：
  ① 裸 `grep -c 'language-mermaid'` 会命中**内联 mermaid.js 源码里自带的同名字符串** → 必须精确到 `class="language-mermaid"`；
  ② `--dump-dom` 的输出还会带上 **mermaid 运行时的同名类** → 残留判定**只能在静态 HTML 上做**；
  ③ **自引用陷阱**：把判据串写进一份文档（例如规划文档把它当成验收判据），再拿这条判据去查**这份文档自己**，
  必然命中自己写的那几个字。**判据串只放在 README/脚本里；产物里只描述、不写字面量。**

## 回归测试

渲染器属于"改了容易悄悄坏"的一类（围栏处理、封面消费、锚点对齐、样式内联）。
新增/修改后必须跑它的测试套：断言的都是**性质**而不是快照——图成了、围栏换干净了、
目录锚点对得上、普通代码块没被顺手拆坏、无浏览器时 `skip` 而不是伪装通过。
