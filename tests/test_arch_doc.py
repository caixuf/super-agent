"""``tools/arch_doc.py`` 回归测试 —— 盯的是「**图真的成了**」，不是「文件生成了」。

两条真踩过的坑，各有一条测试钉住：

1. mermaid 围栏只改开围栏、保留围栏 → markdown 照旧当代码块渲染，页面上是**源码文字**
   （DOM 里 ``data-processed`` 为 0、``language-mermaid`` 残留不为 0）；
2. 截图必须给虚拟时间预算，否则截到的页面里 mermaid **还没画完**。

需要浏览器的端到端那条，缺浏览器时 ``skip``：不把「环境缺失」伪装成「通过」。
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("arch_doc", ROOT / "tools" / "arch_doc.py")
assert _spec and _spec.loader
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


def _browser() -> str | None:
    for b in arch_doc.BROWSERS:
        p = shutil.which(b) or (b if Path(b).exists() else None)
        if p:
            return p
    return None


def test_mermaid_fence_is_swapped_wholesale():
    html = arch_doc.md_to_html(DOC)
    assert '<div class="mermaid">' in html
    assert "A --> B" in html, "mermaid 源码必须在 div 里原样出现"
    assert "A --&gt; B" not in html, "转义了 → 说明它被当成代码块走了 markdown 转义"
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


@pytest.mark.skipif(_browser() is None, reason="本机没有 chromium/google-chrome")
def test_render_end_to_end_produces_selfcontained_html_and_png(tmp_path, monkeypatch):
    monkeypatch.setattr(arch_doc, "VIRTUAL_TIME_BUDGET_MS", 8000)
    html, png = tmp_path / "d.html", tmp_path / "d.png"
    arch_doc.render(html, "T", arch_doc.md_to_html(DOC), min_height=900)
    arch_doc.screenshot(html, png, 900, 900)
    arch_doc.trim(png)
    assert html.stat().st_size > 1000
    assert png.exists() and png.stat().st_size > 5000, "PNG 太小 → 大概率是整页空白"