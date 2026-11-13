#!/usr/bin/env python3
"""Render real CardKit 2.0 payloads (in-progress + terminal) to PNG for visual verification."""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


_TEMPLATE_COLORS = {
    "wathet": ("#e8f3ff", "#1f6feb", "#0b3b75"),
    "indigo": ("#eef0ff", "#3b49df", "#1e2570"),
    "carmine": ("#ffecee", "#cf222e", "#6e0f17"),
}
_BG_COLORS = {
    "turquoise-50": "#eefaf8",
    "blue-50": "#f0f6ff",
    "red-50": "#fff1f2",
    "grey-50": "#f6f8fa",
}


def _md_to_html(md: str) -> str:
    """Minimal deterministic Markdown-to-HTML renderer for CardKit 2.0 verification."""
    lines = (md or "").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[i + 1]):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i]:
                cols = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cols) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        esc = html.escape(line)
        esc = re.sub(r"&lt;font\s+color=(?:&#x27;|&quot;|['\"])([^&'\"]+)(?:&#x27;|&quot;|['\"])&gt;(.*?)&lt;/font&gt;", r"<span style='color:\1;font-size:12px'>\2</span>", esc)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(r"\*(.+?)\*", r"<span class='muted'>\1</span>", esc)
        esc = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc)
        if esc.startswith("&gt; "):
            out.append(f"<blockquote>{esc[5:]}</blockquote>")
        elif esc.startswith("- "):
            out.append(f"<div class='li'>• {esc[2:]}</div>")
        elif esc.strip():
            out.append(f"<p>{esc}</p>")
        i += 1
    return "\n".join(out)


def _render_element_html(el: dict) -> str:
    tag = el.get("tag")
    if tag == "markdown":
        return f"<div class='md'>{_md_to_html(el.get('content', ''))}</div>"
    if tag == "column_set":
        bg = _BG_COLORS.get(el.get("background_style", ""), "#ffffff")
        cols = "".join(
            f"<div class='col'>{''.join(_render_element_html(c) for c in col.get('elements', []))}</div>"
            for col in el.get("columns", [])
        )
        return f"<div class='colset' style='background:{bg}'>{cols}</div>"
    if tag == "collapsible_panel":
        expanded = bool(el.get("expanded", False))
        hdr = _md_to_html((el.get("header") or {}).get("content", ""))
        body = "".join(_render_element_html(c) for c in el.get("elements", []))
        arrow = "▼" if expanded else "▶"
        body_html = f"<div class='panel-body'>{body}</div>" if expanded else "<div class='panel-collapsed-hint'>[已折叠 - 点击展开排查链路]</div>"
        return f"<div class='panel'><div class='panel-hdr'>{arrow} {hdr}</div>{body_html}</div>"
    if tag == "button":
        btn_txt = html.escape(((el.get("text") or {}).get("content")) or "操作")
        return f"<div class='btn-row'><span class='ios-btn'>{btn_txt}</span></div>"
    if tag == "table":
        cols = el.get("columns", [])
        rows = el.get("rows", [])
        th = "".join(f"<th>{html.escape(str(c.get('display_name', c.get('name', ''))))}</th>" for c in cols)
        trs = []
        for r in rows:
            tds = "".join(f"<td>{html.escape(str(r.get(c.get('name', ''), '')))}</td>" for c in cols)
            trs.append(f"<tr>{tds}</tr>")
        return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"
    return ""


def _render_card_html(label: str, card: dict) -> str:
    hdr = card.get("header") or {}
    tpl = hdr.get("template", "wathet")
    hbg, accent, hfg = _TEMPLATE_COLORS.get(tpl, _TEMPLATE_COLORS["wathet"])
    title = html.escape(((hdr.get("title") or {}).get("content")) or "")
    sub = html.escape(((hdr.get("subtitle") or {}).get("content")) or "")
    body_html = "".join(_render_element_html(el) for el in (card.get("body") or {}).get("elements", []))
    return (
        f"<div class='wrap'><div class='badge'>{html.escape(label)}</div>"
        f"<div class='card' style='border-top:4px solid {accent}'>"
        f"<div class='card-hdr' style='background:{hbg};color:{hfg}'>"
        f"<div class='card-title'>{title}</div><div class='card-sub'>{sub}</div></div>"
        f"<div class='card-body'>{body_html}</div></div></div>"
    )


def _load_live_adapter_sample_cards() -> tuple[dict, dict]:
    """Generate real CardKit 2.0 payloads from the patched FeishuAdapter."""
    adapter_path = Path.home() / "super_agent/caixuf/hermes-current/plugins/platforms/feishu/adapter.py"
    import ast
    import time as _time
    from types import SimpleNamespace

    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FeishuAdapter")
    needed = {"_extract_stages_and_clean_text", "_sa_merge_stage_text", "_build_reply_card"}
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in needed]
    mod = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name="Adapter", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
    ns = {"re": re, "time": _time, "Optional": object, "Any": object}
    exec(compile(mod, str(adapter_path), "exec"), ns)
    adapter = ns["Adapter"]()
    adapter._bot_name = "caixuf 高手"

    info: dict = {}
    s1 = adapter._sa_merge_stage_text(info, "正在检索 mff_sync 状态机源码")
    s2 = adapter._sa_merge_stage_text(info, "正在执行 pytest 回归校验 (15 passed)")
    in_progress_card = adapter._build_reply_card(s2, finalize=False, started_at=_time.time() - 6.4)

    final_raw = "结论：P0–P2 门禁全部通过，卡片链路与阶段折叠面板工作正常。\n\n| 检查项 | 状态 | 指标 |\n| :--- | :--- | :--- |\n| 卡片所有权兜底 | PASS | 0 丢包 |\n| 阶段折叠面板 | PASS | 2 步轨迹 |\n| 私有领域召回 | PASS | 5 条 (442ms) |"
    final_merged = adapter._sa_merge_stage_text(info, final_raw, final=True)
    terminal_card = adapter._build_reply_card(final_merged, finalize=True, started_at=_time.time() - 12.8)
    return in_progress_card, terminal_card


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output PNG path")
    parser.add_argument("--send-group", action="store_true", help="Upload screenshot to Feishu test group")
    args = parser.parse_args()

    import base64
    from agentcore.task_card_bridge import TaskCardBridge
    from tools.arch_doc import _browser_module, which_browser

    in_prog_card, term_card = _load_live_adapter_sample_cards()
    with tempfile.TemporaryDirectory() as td:
        bridge = TaskCardBridge(Path(td) / "cards", profile="caixuf", min_update_interval=0.0)
        bridge.bind_task("task-vis-01", origin={"route": "agent:demo:feishu:group:oc_eval"}, goal="验证 P0-P2 阶段进度与结论卡片", role="dev", display_name="研发执行")
        prog_res = bridge.update_progress("task-vis-01", status="running", phase="正在执行回归校验...", progress=0.72, elapsed=8.5)
        task_prog_card = prog_res["card_payload"]

        html_doc = f"""<!doctype html><html data-render-state='ready'><head><meta charset='utf-8'><style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; background:#eef1f6; margin:0; padding:24px; color:#1f2328; }}
        .grid {{ display:grid; grid-template-columns: repeat(3, 1fr); gap:20px; max-width:1440px; margin:0 auto; }}
        .badge {{ font-size:12px; font-weight:600; color:#4b5563; margin-bottom:8px; text-transform:uppercase; }}
        .card {{ background:#fff; border-radius:10px; box-shadow:0 4px 14px rgba(0,0,0,0.08); overflow:hidden; }}
        .card-hdr {{ padding:12px 16px; }}
        .card-title {{ font-size:15px; font-weight:700; }}
        .card-sub {{ font-size:12px; opacity:0.8; margin-top:2px; }}
        .card-body {{ padding:12px 14px; display:flex; flex-direction:column; gap:10px; }}
        .colset {{ border-radius:8px; padding:10px 12px; }}
        .panel {{ border:1px solid #d0d7de; border-radius:8px; overflow:hidden; }}
        .panel-hdr {{ background:#f6f8fa; padding:8px 10px; font-size:12px; font-weight:600; display:flex; gap:6px; align-items:center; }}
        .panel-hdr p {{ margin:0; }}
        .panel-body {{ padding:8px 10px; }}
        .panel-collapsed-hint {{ padding:6px 10px; font-size:11px; color:#6b7280; background:#fafafa; }}
        .md p {{ margin:4px 0; font-size:13px; line-height:1.5; }}
        table {{ width:100%; border-collapse:collapse; margin:6px 0; font-size:12px; background:#fff; }}
        th, td {{ border:1px solid #d0d7de; padding:5px 8px; text-align:left; }}
        th {{ background:#f3f4f6; font-weight:600; }}
        blockquote {{ margin:4px 0; padding:4px 8px; border-left:3px solid #93c5fd; color:#374151; font-size:12px; }}
        code {{ background:#e5e7eb; padding:1px 4px; border-radius:4px; font-size:12px; }}
        .muted {{ color:#6b7280; font-size:12px; }}
        .btn-row {{ display:flex; justify-content:flex-end; }}
        .ios-btn {{ display:inline-block; padding:6px 14px; border-radius:999px; background:#eff6ff; color:#1d4ed8; font-size:12px; font-weight:600; border:1px solid #bfdbfe; }}
        </style></head><body data-theme='ios-cardkit2'><div class='page'><div class='grid'>
        {_render_card_html("1. 前台卡片 · 运行中 (展开排查链路)", in_prog_card)}
        {_render_card_html("2. 前台卡片 · 终态 (自动折叠链路 + 表格结论)", term_card)}
        {_render_card_html("3. 后台任务卡片 · 同卡进度条", task_prog_card)}
        </div></div></body></html>"""
        html_path = Path(td) / "cards_preview.html"
        html_path.write_text(html_doc, encoding="utf-8")
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        browser = which_browser()
        with _browser_module.Browser(browser, 1480, 780, timeout=15) as client:
            client.open(html_path)
            shot = client.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, client.session)
        out_path.write_bytes(base64.b64decode(shot["data"]))
        print(json.dumps({"capture_ok": out_path.exists() and out_path.stat().st_size > 0, "out": str(out_path), "size": out_path.stat().st_size}, ensure_ascii=False))

    if args.send_group:
        from tools.live_group_test import get_tenant_access_token, load_feishu_credentials, upload_and_send_image
        os.environ["HERMES_ENV_PATH"] = str(Path.home() / ".hermes/profiles/caixuf/.env")
        app_id, app_secret = load_feishu_credentials()
        token = get_tenant_access_token(app_id, app_secret)
        img_res = upload_and_send_image(token, str(out_path))
        print(f"Screenshot uploaded to group: code={img_res.get('code')} msg_id={(img_res.get('data') or {}).get('message_id')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
