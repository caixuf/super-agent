#!/usr/bin/env python3
"""arch_doc.py —— 把 Markdown（含 Mermaid）渲染成**带图的**可交付文档。

**定位：解释类 / 架构类回答的默认交付形态。** 这类内容在消息平台里必然被降级
（表格被渲染成源码、图更是没有），所以出口只能是"文档"：
  1) <name>.html  自包含（内联 CSS + 语法高亮样式 + 本地 vendor 的 mermaid），浏览器直接打开
  2) <name>.png   chromium 无头截图（自动裁掉底部留白），用于在 IM/邮件/工单里内联展示

为什么不能只发文本：见 tools/README.md「什么时候自动用」一节。

用法：
    python3 arch_doc.py IN.md [-o OUTDIR] [--title T] [--width 1200]
    python3 arch_doc.py IN.md --lede "一句话摘要" --meta 变更=xxx --meta 版本=v1
    python3 arch_doc.py IN.md --no-cover --no-toc        # 不要封面/目录
    python3 arch_doc.py IN.md --show-path                # 页脚带绝对路径（默认不带，避免外发泄漏用户名）
    python3 arch_doc.py IN.md --png-only | --html-only

依赖（本机已具备）：python-markdown / pygments / Pillow + chromium 或 google-chrome（不需要 node）。
mermaid 走本地 vendor（tools/assets/mermaid.min.js，不进版本库）；缺失时自动拉取，再不行回退 CDN。

几个刻意的设计（踩过坑，别改回去）：
  * 封面会"吃掉"正文第一个 H1 与首段引用 —— 否则同一句话会在封面、目录、正文里出现三次；
  * 页脚默认只写文件名，**不写绝对路径** —— 文档是要外发的，路径里带用户名和内部目录结构；
  * Pygments 的 token 样式必须**内联** —— 只给 class 不内联 CSS，等于没有语法高亮。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
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
:root{
  --fg:#16191d; --body:#2b3138; --muted:#6b7280; --line:#e5e7eb; --bg:#fff; --soft:#f8fafc;
  --accent:#0b5fff; --accent-soft:#eef4ff; --code-bg:#f3f5f8; --code-line:#dde2e8;
}
*{box-sizing:border-box}
body{
  margin:0; padding:60px 64px 84px; background:var(--bg); color:var(--body);
  font:16.5px/1.76 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC",
       "PingFang SC", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
.doc{max-width:1080px; margin:0 auto}
.cover{margin:0 0 34px; padding:0 0 22px; border-bottom:3px solid var(--accent)}
.cover h1{font-size:33px; line-height:1.28; margin:0 0 12px; letter-spacing:-.6px; color:var(--fg)}
.cover .lede{font-size:17px; color:#39404a; margin:0 0 16px; max-width:880px}
.meta{margin:0}
.badge{display:inline-block; font-size:12.5px; color:#28303a; background:#fff;
  border:1px solid var(--line); border-radius:999px; padding:3px 11px; margin:0 8px 8px 0}
.badge.first{background:var(--accent-soft); border-color:#c9dcff; color:#0b3fa8; font-weight:600}
h2{font-size:22.5px; margin:44px 0 14px; padding-left:14px; position:relative; color:var(--fg)}
h2:before{content:""; position:absolute; left:0; top:.18em; bottom:.22em; width:4px;
  border-radius:2px; background:var(--accent)}
h3{font-size:18.5px; margin:29px 0 10px; color:var(--fg)}
h4{font-size:16px; margin:22px 0 8px; color:var(--muted)}
p{margin:12px 0}
ul,ol{margin:12px 0; padding-left:26px}
li{margin:5px 0}
hr{border:0; border-top:1px solid var(--line); margin:34px 0}
a{color:var(--accent); text-decoration:none}
strong{font-weight:650; color:var(--fg)}
code{background:var(--code-bg); padding:.16em .42em; border-radius:6px; font-size:.89em;
  font-family:ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace}
pre{background:var(--code-bg); border:1px solid var(--code-line); border-radius:12px;
  padding:15px 18px; overflow:auto; margin:16px 0}
pre code{background:none; padding:0; font-size:13.2px; line-height:1.62}
table{border-collapse:separate; border-spacing:0; width:100%; margin:18px 0; font-size:14.6px;
  border:1px solid var(--line); border-radius:12px; overflow:hidden}
th,td{border:0; border-bottom:1px solid var(--line); padding:10px 14px; text-align:left; vertical-align:top}
th{background:#eef2f7; color:#111827; font-weight:650}
tbody tr:last-child td{border-bottom:0}
tbody tr:nth-child(even) td{background:#fcfdff}
blockquote{margin:18px 0; padding:6px 18px; border-left:4px solid var(--accent-soft);
  color:#4b5563; background:var(--soft); border-radius:0 10px 10px 0}
.mermaid{margin:22px 0 28px; padding:18px 12px; text-align:center;
  background:#fbfcfe; border:1px solid var(--line); border-radius:12px}
.toc{margin:20px 0 6px; padding:14px 20px; background:var(--soft); border:1px solid var(--line);
  border-radius:12px; font-size:14.5px}
.toc b{display:block; margin:0 0 8px; font-size:12.5px; color:var(--muted); letter-spacing:.06em}
.toc a{display:inline-block; margin:0 16px 6px 0; color:var(--accent)}
.toc ul{list-style:none; margin:0; padding:0}
.toc li{display:inline-block; margin:0 16px 6px 0}
.footer{margin:52px 0 0; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12.5px; line-height:1.75}
"""

HTML_TPL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
{script}
</head><body><div class="doc">
{cover}{toc}{body}
<div class="footer">{footer}</div>
</div>
<script>
  if (window.mermaid) {{ mermaid.initialize({{ startOnLoad:true, theme:"neutral", securityLevel:"loose",
    flowchart:{{ htmlLabels:true, curve:"basis", useMaxWidth:true }},
    sequence:{{ useMaxWidth:true, mirrorActors:false, wrap:true, actorMargin:56 }},
    gantt:{{ useMaxWidth:true, barHeight:22, fontSize:14, topPadding:52, gridLineStartPadding:34 }},
    themeVariables:{{ fontFamily:'-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "PingFang SC", sans-serif',
      fontSize:'15.5px',
      primaryColor:'#eef4ff', primaryBorderColor:'#0b5fff', primaryTextColor:'#0b3fa8',
      lineColor:'#94a3b8', textColor:'#1c2024',
      actorBkg:'#eef4ff', actorBorder:'#0b5fff', actorTextColor:'#0b3fa8',
      signalColor:'#94a3b8', signalTextColor:'#39404a',
      noteBkgColor:'#fff8e6', noteBorderColor:'#ead9a8', noteTextColor:'#6b5733',
      sequenceNumberColor:'#b6c2cf',
      sectionBkgColor:'#f3f5f8', altSectionBkgColor:'#fbfcfe',
      taskBkgColor:'#cfe0ff', taskTextColor:'#0b3fa8', taskTextOutsideColor:'#39404a',
      activeTaskBkgColor:'#0b5fff', activeTaskBorderColor:'#0b5fff',
      doneTaskBkgColor:'#d7ecdd', doneTaskBorderColor:'#a7d7c0',
      gridColor:'#e5e7eb', todayLineColor:'#0b5fff' }} }}); }}
</script>
</body></html>
"""


def _pygments_css() -> str:
    """把 Pygments 的 token 样式内联进页面 —— 不给样式，codehilite 的 span 就是一群灰字。"""
    try:
        from pygments.formatters import HtmlFormatter
        return HtmlFormatter(style="friendly").get_style_defs(".codehilite")
    except Exception:
        return ""


PYGMENTS_CSS = _pygments_css()


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


def inline_md(text: str) -> str:
    """封面这类"单行文本"的行内渲染：只认 `代码` 与 **加粗**，其余一律转义。

    踩过的坑：封面摘要直接 escape 会**把 \\*\\* 星号原样吐出来** —— 校样时一眼假。
    """
    t = _html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


def md_to_html_with_toc(md_text: str) -> tuple[str, str]:
    """返回 (正文 HTML, 目录 HTML)。目录用 markdown 自己生成的 toc —— 锚点因此天然对得上。"""
    try:
        import markdown  # type: ignore
    except ImportError:
        sys.exit("缺少 python-markdown：pip install markdown")
    exts = ["tables", "fenced_code", "codehilite", "sane_lists", "attr_list", "toc", "md_in_html"]
    try:
        md = markdown.Markdown(extensions=exts,
                               extension_configs={"codehilite": {"noclasses": False, "guess_lang": False}})
    except Exception:
        md = markdown.Markdown(extensions=["tables", "fenced_code"])
    body = md.convert(mermaid_to_div(md_text))
    toc = getattr(md, "toc", "") or ""
    if toc:
        toc = toc.replace('<div class="toc">', '<div class="toc"><b>目录</b>', 1)
    return body, toc


def md_to_html(md_text: str) -> str:
    """只要正文（保持这个签名：既有调用方与测试都在用）。"""
    return md_to_html_with_toc(md_text)[0]


def build_cover(title: str, lede: str, metas: list[str]) -> str:
    parts = ['<div class="cover">', f"<h1>{_html.escape(title)}</h1>"]
    if lede:
        parts.append(f'<p class="lede">{inline_md(lede)}</p>')
    if metas:
        badges = "".join(
            f'<span class="badge{" first" if i == 0 else ""}">{inline_md(m)}</span>'
            for i, m in enumerate(metas))
        parts.append(f'<p class="meta">{badges}</p>')
    parts.append("</div>")
    return "\n".join(parts)


def guess_lede(md_text: str) -> str:
    """默认摘要：取文档第一段引用（我们的文档习惯用 `> 一句话` 开头）。"""
    m = re.search(r"^>\s*(.+?)\s*$", md_text, flags=re.M)
    return m.group(1).strip() if m else ""


def strip_cover_consumed(md_text: str, h1: "re.Match[str] | None", lede: str, lede_was_auto: bool) -> str:
    """把已被封面消费掉的 H1 与首段引用从正文里去掉 —— 否则同屏三连重复。"""
    out = md_text
    if h1:
        out = out.replace(h1.group(0), "", 1)
    if lede_was_auto and lede:
        out = re.sub(r"^>[ \t]*" + re.escape(lede) + r"[ \t]*$", "", out, count=1, flags=re.M)
    return out


def render(html_path: Path, title: str, body: str, cover: str = "", toc: str = "", footer: str = "") -> None:
    html_path.write_text(
        HTML_TPL.format(title=_html.escape(title), css=CSS + "\n" + PYGMENTS_CSS,
                        script=load_mermaid_script(), cover=cover, toc=toc, body=body, footer=footer),
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
    ap.add_argument("--lede", default=None, help="封面摘要；默认取正文第一段引用（并把它从正文移走）")
    ap.add_argument("--meta", action="append", default=[], help="封面徽标，可重复：--meta 版本=v1")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--no-toc", action="store_true", help="不生成目录（默认带目录）")
    ap.add_argument("--show-path", action="store_true", help="页脚写绝对路径（默认只写文件名，避免外发泄漏）")
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
    h1 = re.search(r"^#\s+(.+)$", md_text, flags=re.M)
    title = a.title or (h1.group(1).strip() if h1 else stem)

    lede_auto = a.lede is None
    lede = a.lede if a.lede is not None else guess_lede(md_text)
    body_md = md_text if a.no_cover else strip_cover_consumed(md_text, h1, lede, lede_auto)

    body, toc = md_to_html_with_toc(body_md)
    figs = len(_mermaid_blocks(md_text))
    stamped = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cover = "" if a.no_cover else build_cover(
        title, lede, ([f"源文件 {src.name}"] + list(a.meta) + [f"生成 {stamped}"]))
    footer = (f"由 tools/arch_doc.py 渲染 · 源文件 {src.name}"
              + (f"（{src}）" if a.show_path else "")
              + f" · 生成 {stamped} · Mermaid 图 {figs} 张 · 表格与代码块随 HTML 自包含")
    if a.no_toc:
        toc = ""

    html_path, png_path = outdir / f"{stem}.html", outdir / f"{stem}.png"
    if not a.png_only or not html_path.exists():
        render(html_path, title, body, cover=cover, toc=toc, footer=footer)
        print(f"[HTML] {html_path}  ({html_path.stat().st_size} bytes)")
    if a.html_only:
        return
    screenshot(html_path, png_path, a.width, a.height)
    if not a.no_trim:
        trim(png_path)
    print(f"[PNG ] {png_path}  ({png_path.stat().st_size} bytes, mermaid 图 {figs} 张)")


if __name__ == "__main__":
    main()