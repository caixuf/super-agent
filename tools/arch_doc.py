#!/usr/bin/env python3
"""arch_doc.py —— 把 Markdown（含 Mermaid）渲染成*带图的*可交付文档。

产出两件东西：
  1) <name>.html  自包含（内联 CSS + 本地 vendor 的 mermaid.js），浏览器直接打开
  2) <name>.png   chromium 无头截图，用于在 IM/邮件/工单里内联展示

为什么不是简单打印 markdown：
  - 消息平台对表格/图形的渲染能力有限（表格会被降级成源码），架构类内容必须走"文档"这条出口；
  - 截图走 chromium + `--virtual-time-budget`，让 mermaid 有足够虚拟时间把图渲染完再截。

用法：
    python3 arch_doc.py IN.md [-o OUTDIR] [--title T] [--width 1440] [--trim/--no-trim]
    python3 arch_doc.py IN.md --png-only      # 只要图
    python3 arch_doc.py IN.md --html-only     # 只要 HTML

依赖（本机已具备）：node 不需要、python-markdown / pygments / PIL、chromium 或 google-chrome。
mermaid 走本地 vendor（tools/assets/mermaid.min.js）；缺失时回退 CDN。
"""
from __future__ import annotations

import argparse
import html as _html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
MERMAID_LOCAL = ASSETS / "mermaid.min.js"
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"

BROWSERS = [
    "/home/emily/.local/bin/chromium",
    "/home/emily/.local/bin/google-chrome",
    "chromium",
    "google-chrome",
]

# mermaid 是异步渲染：不给虚拟时间预算就会截到「还没画完」的页面。
# 提成常量是为了让测试把它调小（真出图仍用这个值）。
VIRTUAL_TIME_BUDGET_MS = 15000

CSS = """
:root { --fg:#1f2328; --muted:#656d76; --line:#d0d7de; --bg:#fff; --accent:#0969da; --code:#f6f8fa; }
* { box-sizing: border-box; }
body { margin:0; padding:56px 64px 72px; background:var(--bg); color:var(--fg);
       font: 16px/1.72 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif; }
.doc { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 30px; line-height:1.35; margin: 0 0 8px; letter-spacing:-.4px; }
h2 { font-size: 22px; margin: 40px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }
h3 { font-size: 18px; margin: 28px 0 10px; }
h4 { font-size: 16px; margin: 22px 0 8px; color:var(--muted); }
p, li { color: var(--fg); }
.sub { color: var(--muted); font-size: 13.5px; margin: 0 0 26px; }
hr { border:0; border-top:1px solid var(--line); margin: 34px 0; }
a { color: var(--accent); }
code { background: var(--code); padding:.15em .38em; border-radius:6px; font-size:.9em;
       font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace; }
pre { background: var(--code); border:1px solid var(--line); border-radius:10px; padding:14px 16px; overflow:auto; }
pre code { background:none; padding:0; font-size:13px; line-height:1.6; }
table { border-collapse: collapse; width:100%; margin:16px 0; font-size:14.5px; }
th, td { border:1px solid var(--line); padding:9px 12px; text-align:left; vertical-align:top; }
th { background:#f6f8fa; font-weight:600; }
blockquote { margin:16px 0; padding:2px 16px; border-left:4px solid var(--line); color:var(--muted); }
.mermaid { margin: 20px 0 26px; text-align:center; }
.badge { display:inline-block; font-size:12px; color:var(--muted); border:1px solid var(--line);
         border-radius:999px; padding:2px 10px; margin-right:6px; }
"""

HTML_TPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
{script}
</head><body><div class="doc">
{body}
</div>
<script>
  if (window.mermaid) {{ mermaid.initialize({{ startOnLoad:true, theme:"neutral",
      securityLevel:"loose", flowchart:{{ htmlLabels:true, curve:"basis" }},
      themeVariables:{{ fontFamily:'-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "PingFang SC", sans-serif', fontSize:'15px' }} }}); }}
</script>
</body></html>
"""


def load_mermaid_script() -> str:
    """内联本地 vendor 的 mermaid；缺失则回退 CDN（离线场景要保证 PNG 里图能出来）。"""
    ensure_mermaid()
    if MERMAID_LOCAL.exists() and MERMAID_LOCAL.stat().st_size > 100_000:
        return "<script>%s</script>" % MERMAID_LOCAL.read_text(encoding="utf-8", errors="ignore")
    return '<script src="%s"></script>' % MERMAID_CDN


def ensure_mermaid() -> None:
    """首次运行自动把 mermaid 拉到本地（3.5MB 不进仓，见 .gitignore）。

    为什么内联而不是 CDN：截图必须在**没有网**的时候也能出图 —— CDN 断则 PNG 里只剩空框。
    """
    if MERMAID_LOCAL.exists() and MERMAID_LOCAL.stat().st_size > 100_000:
        return
    try:
        import urllib.request
        MERMAID_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(MERMAID_CDN, timeout=25) as r:
            data = r.read()
        if len(data) > 100_000:
            MERMAID_LOCAL.write_bytes(data)
            print(f"[vend] 已拉取 mermaid → {MERMAID_LOCAL} ({len(data)} bytes)")
    except Exception as e:  # 网络不通就退回 CDN，不阻塞渲染
        print(f"[warn] 未能本地化 mermaid（{e}）；将使用 CDN 引用")


def md_to_html(md_text: str) -> str:
    try:
        import markdown  # type: ignore
    except ImportError:
        sys.exit("缺少 python-markdown：pip install markdown")
    md_text = mermaid_to_div(md_text)
    exts = ["tables", "fenced_code", "codehilite", "sane_lists", "attr_list", "toc", "md_in_html"]
    try:
        return markdown.markdown(md_text, extensions=exts,
                                 extension_configs={"codehilite": {"noclasses": False, "guess_lang": False}})
    except Exception:
        return markdown.markdown(md_text, extensions=["tables", "fenced_code"])


def mermaid_to_div(md_text: str) -> str:
    """把 ```mermaid 围栏**换掉**（连围栏一起），而不是塞进代码块里。

    踩过的坑：只把开围栏改成 <div class="mermaid">、保留围栏，markdown 依旧当代码块渲染，
    图上屏是源码文字、mermaid 压根拿不到元素（DOM 里 data-processed 为 0）。
    """
    return re.sub(
        r"^[ \t]*```mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
        lambda m: '<div class="mermaid">\n' + m.group(1).rstrip() + "\n</div>\n",
        md_text, flags=re.S | re.M)


def _mermaid_blocks(md_text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", md_text, flags=re.S)


def render(html_path: Path, title: str, body: str, min_height: int) -> None:
    html_path.write_text(
        HTML_TPL.format(title=_html.escape(title), css=CSS, script=load_mermaid_script(), body=body),
        encoding="utf-8")


def which_browser() -> str:
    for b in BROWSERS:
        p = shutil.which(b) or (b if Path(b).exists() else None)
        if p:
            return p
    sys.exit("找不到 chromium/google-chrome：无法截图（可只出 HTML）")


def screenshot(html_path: Path, png_path: Path, width: int, height: int) -> None:
    """无头截图。--virtual-time-budget 是让 mermaid 渲染完的关键，别省。"""
    browser = which_browser()
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           "--force-device-scale-factor=1.4",  # 高分屏观感，字不糊
           f"--window-size={width},{height}",
           f"--virtual-time-budget={VIRTUAL_TIME_BUDGET_MS}",
           f"--screenshot={png_path}", html_path.as_uri()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not png_path.exists():
        sys.exit("截图失败：%s\n%s" % (" ".join(cmd), (r.stderr or "")[-800:]))


def trim(png_path: Path, pad: int = 24) -> None:
    """裁掉底部大片留白（固定高窗口的副作用），让内联图不浪费版面。"""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    # invert 让"油墨"变成非零，getbbox 直接给出内容范围（避免逐像素下标的类型噪音）
    bbox = ImageChops.invert(im.convert("L")).getbbox()
    if not bbox:
        return
    bottom = min(h, bbox[3] + pad)
    if bottom < h:
        im.crop((0, 0, w, bottom)).save(png_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Markdown(+Mermaid) → 带图文档（HTML + PNG）")
    ap.add_argument("input", help="输入 .md")
    ap.add_argument("-o", "--outdir", default=None, help="输出目录（默认与输入同目录）")
    ap.add_argument("--title", default=None)
    ap.add_argument("--width", type=int, default=1200,
                    help="页面宽度；窄一些图会按容器缩放得更大更易读（默认 1200）")
    ap.add_argument("--height", type=int, default=4200, help="截图窗口高度；多余留白会被自动裁掉")
    ap.add_argument("--no-trim", action="store_true")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--png-only", action="store_true")
    a = ap.parse_args()

    src = Path(a.input).resolve()
    if not src.exists():
        sys.exit("找不到输入：%s" % src)
    md_text = src.read_text(encoding="utf-8")
    outdir = Path(a.outdir).resolve() if a.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    title = a.title or (re.search(r"^#\s+(.+)$", md_text, flags=re.M).group(1).strip() if re.search(r"^#\s+(.+)$", md_text, flags=re.M) else stem)

    html_path, png_path = outdir / f"{stem}.html", outdir / f"{stem}.png"
    if not a.png_only:
        render(html_path, title, md_to_html(md_text), min_height=a.height)
        print(f"[HTML] {html_path}  ({html_path.stat().st_size} bytes)")
    if a.html_only:
        return
    if not html_path.exists():
        render(html_path, title, md_to_html(md_text), min_height=a.height)
    screenshot(html_path, png_path, a.width, a.height)
    if not a.no_trim:
        trim(png_path)
    print(f"[PNG ] {png_path}  ({png_path.stat().st_size} bytes, mermaid 块 {len(_mermaid_blocks(md_text))} 个)")


if __name__ == "__main__":
    main()