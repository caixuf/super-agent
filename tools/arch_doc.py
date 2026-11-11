#!/usr/bin/env python3
"""arch_doc.py —— 把 Markdown（含 Mermaid）渲染成**带图的**可交付文档。

**定位：解释类 / 架构类回答的默认交付形态。** 这类内容在消息平台里必然被降级
（表格被渲染成源码、图更是没有），所以出口只能是"文档"：
  1) <name>.html  自包含（内联 CSS + 语法高亮样式 + 本地 vendor 的 mermaid），浏览器直接打开
  2) <name>.png   Chromium首屏预览；长文另有分页PNG和完整覆盖清单

为什么不能只发文本：见 tools/README.md「什么时候自动用」一节。

用法：
    python3 arch_doc.py IN.md [-o OUTDIR] [--title T] [--width 1200]
    python3 arch_doc.py IN.md --lede "一句话摘要" --meta 变更=xxx --meta 版本=v1
    python3 arch_doc.py IN.md --no-cover --no-toc        # 不要封面/目录
    python3 arch_doc.py IN.md --show-path                # 页脚带绝对路径（默认不带，避免外发泄漏用户名）
    python3 arch_doc.py IN.md --png-only | --html-only

依赖（本机已具备）：python-markdown / pygments / Pillow + chromium 或 google-chrome（不需要 node）。
mermaid 走本地 vendor（tools/assets/mermaid.min.js，不进版本库）；缺失时尝试本地化，失败则明确报错。
截图通过CDP等待图与字体完成，测量实际正文高度；不会用固定窗口截断长文。

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
import importlib.util
import json
import os
import tempfile
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
MERMAID_LOCAL = ASSETS / "mermaid.min.js"
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"

BROWSERS = [
    str(Path.home() / '.local/bin/chrome-headless-shell'),
    str(Path.home() / '.local/bin/chromium-headless-shell'),
    str(Path.home() / '.local/bin/chromium'),
    str(Path.home() / '.local/bin/google-chrome'),
    "chrome-headless-shell",
    "chromium-headless-shell",
    "chromium",
    "google-chrome",
]

_browser_spec = importlib.util.spec_from_file_location('arch_doc_browser', HERE / 'doc_browser.py')
_browser_module = importlib.util.module_from_spec(_browser_spec)
_browser_spec.loader.exec_module(_browser_module)

# 保留旧配置常量作为CDP readiness timeout的下限输入；不再依赖虚拟时间截图。
VIRTUAL_TIME_BUDGET_MS = 15000

CSS = """
:root{
  --fg:#16191d; --body:#2b3138; --muted:#6b7280; --line:#e5e7eb; --bg:#fff; --soft:#f8fafc;
  --accent:#0b5fff; --accent-soft:#eef4ff; --code-bg:#f6f8fa; --code-line:#d0d7de;
}
*{box-sizing:border-box}
body{
  margin:0; padding:48px 58px 58px; background:var(--bg); color:var(--body);
  font:17px/1.8 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", "DejaVu Sans", "Noto Sans CJK SC",
       "PingFang SC", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
}
.doc{max-width:1080px; margin:0 auto}
.cover{margin:0 0 28px; padding:0 0 24px; border-bottom:3px solid var(--accent)}
.cover h1{font-size:36px; line-height:1.3; margin:0 0 14px; letter-spacing:-.6px; color:var(--fg)}
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
pre{background:var(--code-bg); border:1px solid var(--code-line); border-radius:10px;
  padding:15px 18px; overflow-wrap:anywhere; white-space:pre-wrap; margin:16px 0}
pre code{background:none; padding:0; font-size:13.2px; line-height:1.62}
table{border-collapse:separate; border-spacing:0; width:100%; margin:18px 0; font-size:14.6px;
  border:1px solid var(--line); border-radius:12px; overflow:hidden}
th,td{border:0; border-bottom:1px solid var(--line); padding:10px 14px; text-align:left; vertical-align:top}
th{background:#eef2f7; color:#111827; font-weight:650}
tbody tr:last-child td{border-bottom:0}
tbody tr:nth-child(even) td{background:#fcfdff}
blockquote{margin:18px 0; padding:6px 18px; border-left:4px solid var(--accent-soft);
  color:#4b5563; background:var(--soft); border-radius:0 10px 10px 0}
.callout{margin:18px 0; padding:14px 18px; border-radius:10px; border:1px solid var(--line); border-left-width:4px}
.callout-title{font-size:14.5px; font-weight:650; margin:0 0 8px; letter-spacing:.02em}
.callout p{margin:8px 0}
.callout p:first-of-type{margin-top:4px}
.callout p:last-child,.callout ul:last-child,.callout ol:last-child{margin-bottom:0}
.callout-note{background:#f0f6ff; border-color:#c9dcff; border-left-color:#0b5fff}
.callout-note .callout-title{color:#0b5fff}
.callout-tip{background:#f0fdfa; border-color:#99f6e4; border-left-color:#0d9488}
.callout-tip .callout-title{color:#0d9488}
.callout-important{background:#f5f3ff; border-color:#ddd6fe; border-left-color:#6d28d9}
.callout-important .callout-title{color:#6d28d9}
.callout-warning{background:#fffbeb; border-color:#fde68a; border-left-color:#d97706}
.callout-warning .callout-title{color:#d97706}
.callout-caution{background:#fef2f2; border-color:#fecaca; border-left-color:#dc2626}
.callout-caution .callout-title{color:#dc2626}
.mermaid{margin:22px 0 28px; padding:22px 14px; text-align:center;
  background:#fbfcfe; border:1px solid var(--line); border-radius:12px}
.mermaid svg{max-width:100%;height:auto}
.mermaid .nodeLabel,.mermaid text{font-size:16px}
td,th{overflow-wrap:anywhere}
.toc ul ul{display:none}
@media(max-width:700px){body{padding:28px 24px 40px}.cover h1{font-size:29px}table{font-size:14px}}
@media print{body{padding:20px}.cover,.mermaid,.callout,pre,table{break-inside:avoid}h2,h3{break-after:avoid}}
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
  document.documentElement.dataset.renderState='loading';
  (async function() {{ try {{
  const nodes = [...document.querySelectorAll('.mermaid')];
  if (nodes.length && !window.mermaid) throw new Error('Local diagram engine unavailable');
  if (window.mermaid) {{ mermaid.initialize({{ startOnLoad:false, theme:"base", securityLevel:"strict",
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
      gridColor:'#e5e7eb', todayLineColor:'#0b5fff' }} }});
      if(nodes.length) await mermaid.run({{nodes:nodes}});
  }}
  await document.fonts.ready;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  if(nodes.some(node=>!node.querySelector('svg') || node.querySelector('.error-icon,.error-text'))) throw new Error('Diagram did not render');
  document.documentElement.dataset.renderState='ready';
  }} catch(error) {{ document.documentElement.dataset.renderState='error';
    document.documentElement.dataset.renderError=String(error.message||error); }} }})();
</script>
</body></html>
"""


def _pygments_css() -> str:
    """把 Pygments 的 token 样式内联进页面 —— 现代清晰的高对比冷调语法配色（对齐 GitHub Light / VitePress）。"""
    base_css = ""
    try:
        from pygments.formatters import HtmlFormatter
        base_css = HtmlFormatter(style="default").get_style_defs(".codehilite")
    except Exception:
        pass

    modern_overlay = """
/* Modern High-Contrast Code Highlight (GitHub Light / VitePress) */
.codehilite { background: var(--code-bg); border-radius: 10px; }
.codehilite pre { background: var(--code-bg); color: #24292f; margin: 0; padding: 15px 18px; }
.codehilite .c, .codehilite .ch, .codehilite .cm, .codehilite .c1, .codehilite .cs, .codehilite .cpf { color: #6e7781; font-style: italic; }
.codehilite .k, .codehilite .kd, .codehilite .kn, .codehilite .kp, .codehilite .kr, .codehilite .ow { color: #cf222e; font-weight: 600; }
.codehilite .kc, .codehilite .kt { color: #0550ae; font-weight: 600; }
.codehilite .s, .codehilite .sa, .codehilite .sb, .codehilite .sc, .codehilite .dl, .codehilite .sd, .codehilite .s2, .codehilite .se, .codehilite .sh, .codehilite .si, .codehilite .sx, .codehilite .sr, .codehilite .s1, .codehilite .ss { color: #0a3069; }
.codehilite .na, .codehilite .nc, .codehilite .nd, .codehilite .ne, .codehilite .nf, .codehilite .fm { color: #8250df; font-weight: 600; }
.codehilite .nb, .codehilite .bp { color: #0550ae; }
.codehilite .no, .codehilite .nv, .codehilite .vc, .codehilite .vg, .codehilite .vi, .codehilite .vm { color: #953800; }
.codehilite .m, .codehilite .mb, .codehilite .mf, .codehilite .mh, .codehilite .mi, .codehilite .mo, .codehilite .il { color: #0550ae; }
.codehilite .o { color: #24292f; }
.codehilite .hll { background-color: #e8f0fe; }
"""
    return (base_css + "\n" + modern_overlay).strip()


PYGMENTS_CSS = _pygments_css()


def load_mermaid_script() -> str:
    """内联本地引擎；无法本地化时拒绝生成伪自包含HTML。"""
    ensure_mermaid()
    if MERMAID_LOCAL.exists() and MERMAID_LOCAL.stat().st_size > 100_000:
        return "<script>%s</script>" % MERMAID_LOCAL.read_text(encoding="utf-8", errors="ignore")
    raise RuntimeError('Local Mermaid asset unavailable; cannot create a self-contained document')


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
    except Exception as e:
        raise RuntimeError('Unable to provision the local Mermaid asset') from e


def _diagram_fences(md_text, replace=False):
    """One fence parser for conversion and counting; preserve nested examples."""
    lines, out, blocks, index = md_text.splitlines(keepends=True), [], [], 0
    while index < len(lines):
        opening = re.match(r'^[ \t]*(`{3,}|~{3,})([^\r\n]*)', lines[index])
        if not opening:
            out.append(lines[index]); index += 1
            continue
        fence, info = opening.group(1), opening.group(2).strip()
        end = index + 1
        closing = re.compile(r'^[ \t]*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*$')
        while end < len(lines) and not closing.match(lines[end].rstrip('\r\n')):
            end += 1
        if info.lower() == 'mermaid':
            if end == len(lines):
                raise ValueError('Unclosed Mermaid fence')
            block = ''.join(lines[index + 1:end]).rstrip()
            blocks.append(block)
            if replace:
                out.append('<div class="mermaid">\n' + _html.escape(block, quote=False) + '\n</div>\n')
            else:
                out.extend(lines[index:end + 1])
        else:
            out.extend(lines[index:end + 1])
        index = end + 1
    return ''.join(out), blocks


def mermaid_to_div(md_text: str) -> str:
    """把 ```mermaid 围栏**换掉**（连围栏一起），而不是塞进代码块里。

    踩过的坑：只把开围栏改成 <div class="mermaid">、保留围栏，markdown 依旧当代码块渲染，
    图上屏是源码文字、mermaid 压根拿不到元素（DOM 里 data-processed 为 0）。
    """
    return _diagram_fences(md_text, replace=True)[0]


def _mermaid_blocks(md_text: str) -> list[str]:
    return _diagram_fences(md_text)[1]


def inline_md(text: str) -> str:
    """封面这类"单行文本"的行内渲染：只认 `代码` 与 **加粗**，其余一律转义。

    踩过的坑：封面摘要直接 escape 会**把 \\*\\* 星号原样吐出来** —— 校样时一眼假。
    """
    t = _html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return t


CALLOUT_HEADER_RE = re.compile(
    r'^\s*>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](?:[ \t]+([^\r\n]*))?$',
    re.IGNORECASE,
)

DEFAULT_CALLOUT_TITLES = {
    "NOTE": "说明",
    "TIP": "建议",
    "IMPORTANT": "关键",
    "WARNING": "注意",
    "CAUTION": "风险",
}


def callouts_to_div(md_text: str) -> str:
    """将 GFM 语义提示块（[!NOTE]/[!TIP]/[!IMPORTANT]/[!WARNING]/[!CAUTION]）转换为带独立样式的 div 结构。

    围栏感知：代码块内部（``` / ~~~）的提示块语法严格保留源码，不作转换。
    """
    lines = md_text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    list_marker = re.compile(r'^[ \t]*([-*+]|\d+\.)[ \t]+')

    while i < n:
        line = lines[i]
        m_fence = re.match(r'^[ \t]*(`{3,}|~{3,})([^\r\n]*)', line)
        if m_fence:
            fence = m_fence.group(1)
            out.append(line)
            i += 1
            closing = re.compile(r'^[ \t]*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*$')
            while i < n:
                cur = lines[i]
                out.append(cur)
                i += 1
                if closing.match(cur.rstrip('\r\n')):
                    break
            continue

        m_callout = CALLOUT_HEADER_RE.match(line.rstrip('\r\n'))
        if m_callout:
            kind_raw = m_callout.group(1)
            kind_upper = kind_raw.upper()
            kind_lower = kind_raw.lower()
            custom_title = (m_callout.group(2) or "").strip()
            if custom_title:
                title_text = f"{kind_upper} · {custom_title}"
            else:
                title_text = f"{kind_upper} · {DEFAULT_CALLOUT_TITLES.get(kind_upper, kind_upper)}"

            raw_body_lines: list[str] = []
            i += 1
            while i < n:
                cand = lines[i]
                if CALLOUT_HEADER_RE.match(cand.rstrip('\r\n')):
                    break
                m_quote = re.match(r'^[ \t]*>(?:[ \t]?(.*))?$', cand.rstrip('\r\n'))
                if m_quote:
                    raw_body_lines.append(m_quote.group(1) or "")
                    i += 1
                else:
                    break

            body_formatted: list[str] = []
            in_body_fence = False
            bf_char = ''
            bf_len = 0
            for bline in raw_body_lines:
                bf_m = re.match(r'^[ \t]*(`{3,}|~{3,})', bline)
                if bf_m:
                    fc = bf_m.group(1)
                    if not in_body_fence:
                        in_body_fence = True
                        bf_char = fc[0]
                        bf_len = len(fc)
                    elif fc[0] == bf_char and len(fc) >= bf_len:
                        in_body_fence = False
                if not in_body_fence and list_marker.match(bline):
                    if body_formatted and body_formatted[-1].strip() and not list_marker.match(body_formatted[-1]):
                        body_formatted.append('')
                body_formatted.append(bline)

            body_str = '\n'.join(body_formatted).strip('\n')
            block_lines = [
                f'<div class="callout callout-{kind_lower}" markdown="1">',
                f'<div class="callout-title">{inline_md(title_text)}</div>',
            ]
            if body_str:
                block_lines.append('')
                block_lines.append(body_str)
            block_lines.append('</div>\n')

            out.append('\n'.join(block_lines))
            continue

        out.append(line)
        i += 1

    return ''.join(out)


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
        md = markdown.Markdown(extensions=["tables", "fenced_code", "md_in_html"])
    body = md.convert(mermaid_to_div(callouts_to_div(md_text)))
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
    """默认摘要：取文档第一段普通引用（跳过代码围栏与五类语义提示块）。"""
    lines = md_text.splitlines()
    in_fence = False
    fence_char = ""
    fence_len = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m_fence = re.match(r'^[ \t]*(`{3,}|~{3,})', line)
        if m_fence:
            fc = m_fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char = fc[0]
                fence_len = len(fc)
            elif fc[0] == fence_char and len(fc) >= fence_len:
                in_fence = False
            i += 1
            continue
        if in_fence:
            i += 1
            continue

        if CALLOUT_HEADER_RE.match(line):
            i += 1
            while i < n and re.match(r'^[ \t]*>', lines[i]):
                i += 1
            continue

        m = re.match(r'^[ \t]*>[ \t]*(.+?)[ \t]*$', line)
        if m:
            return m.group(1).strip()
        i += 1
    return ""


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
                        script=load_mermaid_script() if '<div class="mermaid">' in body else '',
                        cover=cover, toc=toc, body=body, footer=footer),
        encoding="utf-8")


def which_browser() -> str:
    return _browser_module.find_browser(BROWSERS)


def screenshot(html_path: Path, png_path: Path, width: int, height: int) -> dict:
    """CDP等待渲染就绪后完整截图；返回含分页范围与质量指标的manifest。"""
    return _browser_module.capture_document(html_path, png_path, width, height,
                                            browser=which_browser(), timeout=max(15, VIRTUAL_TIME_BUDGET_MS / 1000))


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
    ap.add_argument("--height", type=int, default=1800, help="浏览器初始视口高度；全文高度自动测量，长文完整分页")
    ap.add_argument("--no-trim", action="store_true", help="兼容旧参数；现已按实际正文高度截图，无需裁白")
    format_group = ap.add_mutually_exclusive_group()
    format_group.add_argument("--html-only", action="store_true")
    format_group.add_argument("--png-only", action="store_true")
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
    cover = "" if a.no_cover else build_cover(title, lede, list(a.meta))
    footer = f"生成 {stamped}" + (f" · 源文件 {src}" if a.show_path else "")
    if a.no_toc:
        toc = ""

    html_path, png_path = outdir / f"{stem}.html", outdir / f"{stem}.png"
    fd, temporary = tempfile.mkstemp(prefix='.' + stem + '.', suffix='.html', dir=outdir)
    os.close(fd)
    try:
        staged_html = Path(temporary)
        render(staged_html, title, body, cover=cover, toc=toc, footer=footer)
        if a.html_only:
            os.replace(staged_html, html_path)
            print(f"[HTML] {html_path}  ({html_path.stat().st_size} bytes)")
            return
        manifest = screenshot(staged_html, png_path, a.width, a.height)
        manifest['source_html'] = html_path.name if not a.png_only else None
        manifest['source_markdown_sha256'] = __import__('hashlib').sha256(md_text.encode()).hexdigest()
        metadata_path = png_path.with_suffix('.render.json')
        metadata_fd, metadata_tmp = tempfile.mkstemp(prefix='.' + stem + '.', suffix='.json', dir=outdir)
        try:
            with os.fdopen(metadata_fd, 'w') as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(metadata_tmp, metadata_path)
        finally:
            if Path(metadata_tmp).exists():
                Path(metadata_tmp).unlink()
        if not a.png_only:
            os.replace(staged_html, html_path)
            print(f"[HTML] {html_path}  ({html_path.stat().st_size} bytes)")
        print(f"[PNG ] {png_path}  ({png_path.stat().st_size} bytes, {len(manifest['pages'])} page(s), {figs} diagram(s))")
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


if __name__ == "__main__":
    main()
