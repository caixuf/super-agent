# tools/ —— 智能体自带的小工具

## `arch_doc.py`：Markdown（含 Mermaid）→ 带图文档

架构类内容在 IM / 工单里天然被降级（表格会被渲染成源码、图更是没有），**出口只能是"文档"**。
这个脚本把一份 Markdown 变成两件可交付物：

| 产物 | 是什么 | 给谁看 |
| :--- | :--- | :--- |
| `<名>.html` | 自包含：内联 CSS + 本地 mermaid | 人，双击即可读；可直接进文档系统 |
| `<名>.png` | chromium 无头截图（自动裁掉底部留白） | IM / 邮件 / 工单内联展示，对方零环境 |

```bash
python3 tools/arch_doc.py doc.md -o out/          # HTML + PNG
python3 tools/arch_doc.py doc.md --png-only       # 只要图
python3 tools/arch_doc.py doc.md --width 1100     # 图要更大就收窄页宽
python3 tools/arch_doc.py doc.md --html-only      # 只要 HTML
```

### 机制与坑（改之前先看）

- **mermaid 必须内联进 HTML，不能用 CDN**：截图的正确性不能依赖出网，CDN 断则 PNG 里只剩空框。
  首次运行会把 `mermaid.min.js`（约 3.5MB）拉到 `tools/assets/`，该文件已进 `.gitignore`。
- **围栏要连围栏一起换掉**：把 ` ```mermaid ` 只改成 `<div class="mermaid">` 而保留围栏，
  markdown 仍按代码块渲染 → 图上屏是源码文字。判定「图真的成了」看 DOM 里
  `data-processed="true"` 的个数，以及 `language-mermaid` 残留必须为 0。
- **截图要用 `--virtual-time-budget`**：mermaid 是异步渲染，不给虚拟时间预算就会截到还没画完的页面。
- 依赖：`python-markdown` / `pygments` / `Pillow` + 本机 chromium 或 google-chrome（node 不需要）。
- 只支持 Mermaid。需要 Graphviz / PlantUML 这类更强排版时，再单独加后端（本机已有 java，PlantUML 可行）。

### 验证方式（别只看"文件生成了"）

```bash
# 图真的渲成了吗 —— 两条互补，都要过
chromium --headless=new --virtual-time-budget=15000 --dump-dom out/doc.html \
  | grep -c 'data-processed="true"'          # 应等于 mermaid 块数（这个只有 DOM 里可见）
grep -o 'class="language-mermaid"' out/doc.html | wc -l    # 应为 0（这个必须在**静态 HTML 文件**上查）
```

> 两条口径的边界（都实测踩过）：
> - 内联的 mermaid.js 源码里带 `language-mermaid` 字符串 → 裸 `grep -c 'language-mermaid'` 假阳性；
> - `dump-dom` 的输出还会带上 mermaid 运行时的同名类 → 残留判定**只能在静态 HTML 上做**，否则同样是假阳性。
> 反过来 `data-processed` 只在 DOM 里出现，静态文件里没有 —— 所以这两条不能互相替代。
> - **自引用陷阱**：把判据串写进一份"随便哪个产物"（比如规划文档把它当验收判据）后，再拿这条判据去查**这份文档自己**，
>   必然命中自己写的那几个字 —— 实测连续误判两次。**判据串要么只放在 README/脚本里，要么在产物里描述而不写字面量。**