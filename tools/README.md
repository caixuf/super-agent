# 图文交付工具

`arch_doc.py` 把 Markdown 与 Mermaid 生成完整、自包含的 HTML 和可阅读的 PNG 页。
它复用同一套排版与浏览器检查，不另开一条“看起来成功”的截图路径。

## 什么时候生成

需要读者理解职责和关系、一个过程、分阶段行动或方案取舍时，图文通常有帮助。
一个事实、术语定义、状态查询、单条命令和局部追问，直接回答更合适。
明确的格式偏好优先；不能因为有一个表格或篇幅稍长就自动生成附件。

完整判断与写作要求见 `skills/illustrated-doc-rendering/SKILL.md`。
`illustrated-delivery` 插件可以在每轮给模型临时建议，最终仍要由模型按意思选规模、写清楚内容并实际调用工具。

## 使用

```bash
python3 tools/arch_doc.py doc.md -o output/ --width 1100
python3 tools/arch_doc.py doc.md -o output/ --png-only
python3 tools/arch_doc.py doc.md -o output/ --html-only
```

在业务工作目录使用时，用管理员配置的本体绝对路径；也可调用技能 wrapper：

```bash
bash /path/to/skill/scripts/render_doc.sh --engine-root /path/to/engine doc.md -o output/
bash /path/to/skill/scripts/verify_render.sh output/doc.html 2 --engine-root /path/to/engine
```

wrapper 支持源码仓和托管安装路径；自定义安装位置可用 `--engine-root` 或 `ARCH_DOC_TOOL`。
不扫描个人目录、不假定用户名。

## 产物与质量

- `doc.html` 保存完整内容与本地内联 Mermaid，不依赖在线脚本。
- `doc.png` 是第一页预览；需要时增加 `doc.page-02.png` 等连续页，避免整份长文缩成小字。
- `doc.render.json` 记录本次源文指纹、实际高度、图数、页范围与完整性。
- `--png-only` 仍重新读取当前 Markdown；旧文件存在不代表本次渲染成功。
- `--html-only` 可以不启动浏览器；要验证交付效果，仍需执行浏览器检查与视觉校样。

浏览器等待图形、字体和布局就绪，再检查真实 SVG、源码残留、横向溢出与图中文字尺寸。
分页尽量保留图、表和标题的阅读关系。错误图或不完整截图会报错，不复用旧 PNG 冒充成功。

检查器的四例自检：

```bash
bash skills/illustrated-doc-rendering/scripts/selftest_verify_render.sh
```

四例包含正常页面、真实代码残留、普通文字自引用、图数不符。两个负例必须失败。
机械检查通过后还要看实际 PNG，检查阅读顺序、字号、密度和正文是否好懂。

## 依赖与边界

Python 可选依赖通过 `pip install '.[documents]'` 声明：markdown、Pygments、Pillow、websocket-client。
浏览器由 `ARCH_DOC_BROWSER`、PATH 或当前用户的标准本地 bin 位置发现。
Mermaid vendor 首次需要准备到 `tools/assets/mermaid.min.js`；生成时内联，缺失且无法准备就明确失败，不悄悄挂 CDN。

插件、技能、renderer、依赖和 vendor 都需要进入最终部署清单。完整打包与 Docker 干净环境复现按用户要求留到所有功能完成后的最后阶段。
