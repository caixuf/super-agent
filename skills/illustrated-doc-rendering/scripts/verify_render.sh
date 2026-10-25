#!/usr/bin/env bash
# 渲染自检：证明「图真的渲染成图形了」，而不是「文件生成了」。
# 用法: verify_render.sh <渲染出的 .html> <期望的 mermaid 图数量>
# 退出码：0 = 通过；1 = 有图没渲染成 SVG（仍当代码块/RTL 空白）。
set -u

HTML="${1:?用法: verify_render.sh <doc.html> <期望图数>}"
WANT="${2:?用法: verify_render.sh <doc.html> <期望图数>}"

BROWSER="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
if [ -z "$BROWSER" ]; then
  echo "SKIP: 本机没有 chromium/google-chrome，无法自检（不要当成通过）" >&2
  exit 2
fi

DOM="$("$BROWSER" --headless=new --disable-gpu --no-sandbox \
      --virtual-time-budget=15000 --dump-dom "$HTML" 2>/dev/null)"

# 判据 ①：渲成了几张图。`data-processed` 只有 DOM 里有，所以必须在 DOM 上数。
GOT="$(printf '%s' "$DOM" | grep -o 'data-processed="true"' | wc -l)"

# 判据 ②：还剩几个 mermaid 代码块。**关键在 pattern 的形状** —— 三个假阳性都来自它写宽了：
#   - 裸 'language-mermaid' 会命中**内联 mermaid.js 源码里自带的同名字符串**；
#   - 直接数 'class="language-mermaid"' 又会命中**文档里引用这条判据的那段文字**（自引用）；
#   - 只在 --dump-dom 上查，还会带上 mermaid 运行时的同名类。
# 只认「class 作为标签属性」的形态：`<code|pre|div|span ... class="language-mermaid"`。
# 写成文字的、运行时加的类都跨不过 `>`，所以这条 pattern 在静态 HTML 与 DOM 上都精确。
PAT='<(code|pre|div|span)[^>]*class="language-mermaid"'
LEFT="$(printf '%s' "$DOM" | grep -oE "$PAT" | wc -l)"
LEFT_STATIC="$(grep -oE "$PAT" "$HTML" | wc -l)"

printf '  渲染成图: %s (期望 %s)\n  mermaid 代码块残留: DOM=%s 静态=%s (期望 0/0)\n' \
  "$GOT" "$WANT" "$LEFT" "$LEFT_STATIC"

if [ "$GOT" != "$WANT" ] || [ "$LEFT" != "0" ] || [ "$LEFT_STATIC" != "0" ]; then
  echo "FAIL: 图没有全部渲染成 SVG —— 检查 mermaid 围栏是否连围栏一起换成了 <div class=\"mermaid\">" >&2
  exit 1
fi
echo "PASS: 图已渲染"
