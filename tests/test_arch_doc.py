"""``tools/arch_doc.py`` 回归测试 —— 盯的是「**图真的成了**」，不是「文件生成了」。

两条真踩过的坑，各有一条测试钉住：

1. mermaid 围栏只改开围栏、保留围栏 → markdown 照旧当代码块渲染，页面上是**源码文字**
   （DOM 里 ``data-processed`` 为 0、``language-mermaid`` 残留不为 0）；
2. 截图必须给虚拟时间预算，否则截到的页面里 mermaid **还没画完**。

需要浏览器的端到端那条，缺浏览器时 ``skip``：不把「环境缺失」伪装成「通过」。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("arch_doc", ROOT / "tools" / "arch_doc.py")
assert _spec and _spec.loader
# tools/arch_doc.py 的可选依赖是 python-markdown：缺它时相关函数在**调用期**主动退出（不是 import 期）。
# 所以判定要在模块级、按依赖是否存在整组 skip —— 缺可选依赖 ≠ 渲染链坏了，更不能把干净室带成红。
if importlib.util.find_spec("markdown") is None:
    pytest.skip("缺 python-markdown，整组跳过（可选依赖缺失，不是渲染链故障）",
                allow_module_level=True)
arch_doc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arch_doc)

DOC = """# 标题

```mermaid
flowchart LR
  A --> B
```

```bash
echo hi
```
"""


def _capture_ready() -> bool:
    """浏览器截图还要 ``websocket-client`` —— 两者任一缺失都是环境缺失，不是渲染链故障。"""
    return _browser() is not None and importlib.util.find_spec("websocket") is not None


def _browser() -> str | None:
    for b in arch_doc.BROWSERS:
        p = shutil.which(b) or (b if Path(b).exists() else None)
        if p:
            return p
    return None


def test_mermaid_fence_is_swapped_wholesale():
    html = arch_doc.md_to_html(DOC)
    assert '<div class="mermaid">' in html
    assert "A --&gt; B" in html, "diagram source is escaped as text, then read by the engine"
    assert "language-mermaid" not in html, "围栏没换干净 → 图会被当代码块渲染成源码"
    # 非 mermaid 的代码块必须照旧（别为了修图把代码块也拆了）
    assert 'class="codehilite"' in html and "echo" in html and "hi" in html


def test_html_inlines_local_mermaid_instead_of_cdn(tmp_path, monkeypatch):
    vendor = tmp_path / "mermaid.min.js"
    vendor.write_text("window.mermaid = {initialize(){}};" + "x" * 120_000, encoding="utf-8")
    monkeypatch.setattr(arch_doc, "MERMAID_LOCAL", vendor)
    script = arch_doc.load_mermaid_script()
    assert "<script>window.mermaid" in script
    assert "cdn.jsdelivr.net" not in script, "本地有货还挂 CDN → 离线截图会只剩空框"


def test_ensure_mermaid_does_not_touch_existing_file(tmp_path, monkeypatch):
    vendor = tmp_path / "mermaid.min.js"
    vendor.write_text("x" * 120_000, encoding="utf-8")
    monkeypatch.setattr(arch_doc, "MERMAID_LOCAL", vendor)
    before = vendor.stat().st_mtime_ns
    arch_doc.ensure_mermaid()  # 已有则不应触网、不应改写
    assert vendor.stat().st_mtime_ns == before


def test_trim_drops_trailing_blank_but_keeps_ink(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    png = tmp_path / "t.png"
    im = Image.new("RGB", (200, 1200), "white")
    for x in range(20, 120):
        im.putpixel((x, 100), (0, 0, 0))
    im.save(png)
    arch_doc.trim(png, pad=20)
    assert Image.open(png).size[1] <= 200, "底部留白没裁掉"


def test_cover_toc_and_footer_are_wired_and_anchors_match():
    body, toc = arch_doc.md_to_html_with_toc(DOC)
    assert 'class="toc"' in toc and "目录" in toc
    cover = arch_doc.build_cover("标 < 题", "一句话摘要", ["源文件 a.md", "生成 2026-10-26"])
    assert "标 &lt; 题" in cover, "标题里的尖括号必须转义"
    assert cover.count('class="badge') == 2, "两枚徽标（首枚带 first 类）"
    assert 'class="badge first"' in cover and "一句话摘要" in cover
    # 目录锚点必须能在正文里找到对应 id —— 否则点过去是死链
    ids = re.findall(r'href="#([^"]+)"', toc)
    assert ids, "目录里至少应有一个条目"
    for i in ids:
        assert f'id="{i}"' in body, f"目录指向 #{i}，但正文没有这个 id"


def test_guess_lede_takes_first_quote():
    assert arch_doc.guess_lede("> 一句话摘要\n\n正文") == "一句话摘要"
    assert arch_doc.guess_lede("没有引用") == ""


def test_pygments_styles_are_inlined_and_cover_eats_h1():
    """两个都踩过的坑：① 只给 class 不内联 token 样式 = 没有语法高亮；② 封面吃掉 H1/首引 防三连重复。"""
    assert ".codehilite" in arch_doc.PYGMENTS_CSS, "Pygments 样式没内联，代码块不会有语法高亮"
    md = "# 标题\n\n> 摘要一句\n\n## 小节\n"
    h1 = re.search(r"^#\s+(.+)$", md, flags=re.M)
    stripped = arch_doc.strip_cover_consumed(md, h1, "摘要一句", lede_was_auto=True)
    assert "标题" not in stripped and "摘要一句" not in stripped
    assert "## 小节" in stripped, "只许吃被封面消费的部分，别多删"
    # 手动给了 --lede 时，正文里的引用**不该**被删（那是作者有意保留的正文）
    keep = arch_doc.strip_cover_consumed(md, h1, "手工摘要", lede_was_auto=False)
    assert "摘要一句" in keep


def test_inline_md_renders_bold_and_escapes_rest():
    out = arch_doc.inline_md("**粗** 与 `码` 与 <script>")
    assert "<strong>粗</strong>" in out and "<code>码</code>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out


def test_diagram_parser_preserves_nested_fenced_examples():
    example = '````markdown\n```mermaid\nA --> B\n```\n````\n'
    assert arch_doc._mermaid_blocks(example) == []
    assert arch_doc.mermaid_to_div(example) == example
    genuine = '  ```mermaid  \nflowchart LR\nA --> B\n  ```\n'
    assert len(arch_doc._mermaid_blocks(genuine)) == 1
    assert '<div class="mermaid">' in arch_doc.mermaid_to_div(genuine)


def test_missing_vendor_never_silently_uses_network_script(tmp_path, monkeypatch):
    monkeypatch.setattr(arch_doc, 'MERMAID_LOCAL', tmp_path / 'missing.js')
    monkeypatch.setattr(arch_doc, 'ensure_mermaid', lambda: None)
    with pytest.raises(RuntimeError, match='self-contained'):
        arch_doc.load_mermaid_script()


@pytest.mark.skipif(not _capture_ready(), reason="本机没有 chromium/google-chrome 或 websocket-client")
def test_render_end_to_end_produces_selfcontained_html_and_png(tmp_path, monkeypatch):
    monkeypatch.setattr(arch_doc, "VIRTUAL_TIME_BUDGET_MS", 8000)
    html, png = tmp_path / "d.html", tmp_path / "d.png"
    body, toc = arch_doc.md_to_html_with_toc(DOC)
    arch_doc.render(html, "T", body, cover=arch_doc.build_cover("T", "摘要", ["源文件 d.md"]),
                    toc=toc, footer="由 tools/arch_doc.py 渲染")
    arch_doc.screenshot(html, png, 900, 900)
    arch_doc.trim(png)
    assert html.stat().st_size > 1000
    assert png.exists() and png.stat().st_size > 5000, "PNG 太小 → 大概率是整页空白"
    assert 'class="footer"' in html.read_text(encoding="utf-8")


def test_callouts_render_five_types_with_custom_title_and_inner_markdown():
    md = """
> [!NOTE]
> 这是普通说明，包含 **加粗文字** 与 `行内代码`。
> - 列表条目 1
> - 列表条目 2

> [!TIP]
> 这是建议。

> [!IMPORTANT]
> 这是关键提醒。

> [!WARNING] 并发死锁边界
> 这是自定义标题的警告。

> [!CAUTION] 风险操作
> 这是风险提示。
"""
    html = arch_doc.md_to_html(md)
    assert 'class="callout callout-note"' in html
    assert 'class="callout callout-tip"' in html
    assert 'class="callout callout-important"' in html
    assert 'class="callout callout-warning"' in html
    assert 'class="callout callout-caution"' in html

    assert 'class="callout-title"' in html
    assert 'NOTE · 说明' in html
    assert 'TIP · 建议' in html
    assert 'IMPORTANT · 关键' in html
    assert 'WARNING · 并发死锁边界' in html
    assert 'CAUTION · 风险操作' in html

    assert '<strong>加粗文字</strong>' in html
    assert '<code>行内代码</code>' in html
    assert '<ul>' in html and '<li>列表条目 1</li>' in html and '<li>列表条目 2</li>' in html
    assert '**加粗文字**' not in html


def test_code_fences_protect_callouts_from_conversion():
    md = """```markdown
> [!WARNING] 代码块内的告警
> 这里是示例代码，不应转成 callout
```

````markdown
```markdown
> [!NOTE] 嵌套围栏里的提示
```
````

~~~bash
> [!CAUTION] 波浪线围栏
~~~
"""
    html = arch_doc.md_to_html(md)
    assert 'class="callout' not in html
    assert 'callout-warning' not in html
    assert 'callout-note' not in html
    assert 'callout-caution' not in html
    assert '[!WARNING] 代码块内的告警' in html
    assert '[!NOTE] 嵌套围栏里的提示' in html
    assert '!CAUTION' in html and '波浪线围栏' in html


def test_guess_lede_skips_callouts_and_preserves_regular_quote():
    md1 = """# 架构方案

> [!IMPORTANT] 核心警示
> 这是正文告警，绝不是封面摘要！
> 告警的第二行。

> 这是文档真正的一句话摘要。

正文内容。
"""
    assert arch_doc.guess_lede(md1) == "这是文档真正的一句话摘要。"

    md2 = """# 架构方案

> [!TIP] 实用建议
> 仅此一个提示块。

正文。
"""
    assert arch_doc.guess_lede(md2) == ""

    md3 = "> 普通一句话总结\n\n正文"
    assert arch_doc.guess_lede(md3) == "普通一句话总结"

    md4 = "```markdown\n> [!NOTE]\n> 代码里的引用\n```\n\n> 真正的正文摘要"
    assert arch_doc.guess_lede(md4) == "真正的正文摘要"


def test_callout_css_and_print_styles():
    assert ".callout{" in arch_doc.CSS
    assert ".callout-title{" in arch_doc.CSS
    assert ".callout-note{" in arch_doc.CSS and "#0b5fff" in arch_doc.CSS and "#f0f6ff" in arch_doc.CSS
    assert ".callout-tip{" in arch_doc.CSS and "#0d9488" in arch_doc.CSS and "#f0fdfa" in arch_doc.CSS
    assert ".callout-important{" in arch_doc.CSS and "#6d28d9" in arch_doc.CSS and "#f5f3ff" in arch_doc.CSS
    assert ".callout-warning{" in arch_doc.CSS and "#d97706" in arch_doc.CSS and "#fffbeb" in arch_doc.CSS
    assert ".callout-caution{" in arch_doc.CSS and "#dc2626" in arch_doc.CSS and "#fef2f2" in arch_doc.CSS
    assert ".callout" in arch_doc.CSS.split("@media print")[1]
    assert "break-inside:avoid" in arch_doc.CSS.split("@media print")[1]
    assert "--code-bg:#f6f8fa" in arch_doc.CSS
    assert ".codehilite" in arch_doc.PYGMENTS_CSS

