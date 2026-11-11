#!/usr/bin/env python3
"""High-performance headless browser capture with zero proxy stall.

Features:
- Instant launch (<0.5s execution for localhost / local files)
- Force --no-proxy-server to bypass local SOCKS/HTTP proxies for loopback & local services
- DOM text and page title extraction alongside clean PNG screenshot
- Robust fallback discovery across system Chrome, Playwright caches, and standalone shells
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional


_CANDIDATE_PATHS = [
    Path("/usr/local/bin/chrome-headless-shell"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/google-chrome-stable"),
    Path("/usr/bin/chromium"),
    Path("/usr/bin/chromium-browser"),
    Path(os.path.expanduser("~/.local/bin/chrome-headless-shell")),
]

_PLAYWRIGHT_GLOB_PATTERNS = [
    os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    os.path.expanduser("~/.hermes/tools/chromium-*/chrome-linux64/chrome"),
]


def resolve_browser_binary() -> Optional[Path]:
    """Find the best available Chromium or Headless Shell binary."""
    # 1. Check known candidate paths
    for p in _CANDIDATE_PATHS:
        if p.exists() and os.access(p, os.X_OK):
            return p

    # 2. Check Playwright and Hermes tool caches
    import glob
    for pattern in _PLAYWRIGHT_GLOB_PATTERNS:
        matches = glob.glob(pattern)
        for m in sorted(matches, reverse=True):
            mp = Path(m)
            if mp.exists() and os.access(mp, os.X_OK):
                return mp

    # 3. Check system PATH
    for name in ("chrome-headless-shell", "google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


def sanitize_text(text: str, max_chars: int = 2000) -> str:
    """Clean up extracted HTML/DOM text for LLM consumption."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + " ... [truncated]"
    return text


def capture(
    url_or_path: str,
    output_path: Optional[str | Path] = None,
    width: int = 1920,
    height: int = 1080,
    timeout: float = 10.0,
    extract_text: bool = True,
    browser_bin: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Capture a screenshot and optional DOM summary of the given target."""
    start_time = time.monotonic()

    binary = Path(browser_bin) if browser_bin else resolve_browser_binary()
    if not binary or not binary.exists():
        return {
            "success": False,
            "error": "No usable Chromium or chrome-headless-shell binary found",
            "elapsed_seconds": round(time.monotonic() - start_time, 3),
        }

    # Normalize target URL
    target = url_or_path
    if not (target.startswith("http://") or target.startswith("https://") or target.startswith("data:") or target.startswith("file://")):
        p = Path(target).resolve()
        if p.exists():
            target = f"file://{p}"
        else:
            target = f"http://{target}"

    out_file = Path(output_path).resolve() if output_path else Path(tempfile.mktemp(prefix="web_capture_", suffix=".png"))
    out_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(binary),
        "--headless=new" if "headless-shell" not in binary.name else "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-software-rasterizer",
        "--no-proxy-server",
        f"--window-size={width},{height}",
        f"--screenshot={out_file}",
        target,
    ]

    try:
        env = os.environ.copy()
        # Strip proxy env vars so Chromium child processes don't get trapped
        for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(var, None)

        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )

        elapsed = round(time.monotonic() - start_time, 3)

        if not out_file.exists() or out_file.stat().st_size == 0:
            stderr_preview = proc.stderr.decode("utf-8", errors="replace")[:400]
            return {
                "success": False,
                "target": target,
                "error": f"Screenshot file was not created (code={proc.returncode}): {stderr_preview}",
                "elapsed_seconds": elapsed,
            }

        file_size = out_file.stat().st_size
        title = ""
        visible_text = ""

        if extract_text:
            dom_cmd = [
                str(binary),
                "--headless=new" if "headless-shell" not in binary.name else "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--no-proxy-server",
                "--dump-dom",
                target,
            ]
            try:
                dom_proc = subprocess.run(dom_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, env=env)
                if dom_proc.stdout:
                    dom_html = dom_proc.stdout.decode("utf-8", errors="replace")
                    m_title = re.search(r"<title[^>]*>(.*?)</title>", dom_html, re.IGNORECASE | re.DOTALL)
                    if m_title:
                        title = m_title.group(1).strip()
                    visible_text = sanitize_text(dom_html)
            except Exception:
                pass

        return {
            "success": True,
            "target": target,
            "screenshot_path": str(out_file),
            "file_size_bytes": file_size,
            "width": width,
            "height": height,
            "title": title,
            "visible_text_snippet": visible_text,
            "elapsed_seconds": round(time.monotonic() - start_time, 3),
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "target": target,
            "error": f"Timed out after {timeout}s",
            "elapsed_seconds": round(time.monotonic() - start_time, 3),
        }
    except Exception as e:
        return {
            "success": False,
            "target": target,
            "error": str(e),
            "elapsed_seconds": round(time.monotonic() - start_time, 3),
        }


def main():
    parser = argparse.ArgumentParser(description="Zero-lag headless browser screenshot & DOM extractor")
    parser.add_argument("target", help="URL, local HTML file path, or address to capture")
    parser.add_argument("-o", "--output", help="Output PNG path", default=None)
    parser.add_argument("-W", "--width", type=int, default=1920, help="Viewport width (default: 1920)")
    parser.add_argument("-H", "--height", type=int, default=1080, help="Viewport height (default: 1080)")
    parser.add_argument("-t", "--timeout", type=float, default=10.0, help="Timeout in seconds (default: 10.0)")
    parser.add_argument("--no-text", action="store_true", help="Skip DOM text extraction")
    parser.add_argument("--json", action="store_true", help="Print structured JSON output")

    args = parser.parse_args()
    res = capture(
        url_or_path=args.target,
        output_path=args.output,
        width=args.width,
        height=args.height,
        timeout=args.timeout,
        extract_text=not args.no_text,
    )

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        if res.get("success"):
            print(f"[OK] Captured: {res['screenshot_path']} ({res['file_size_bytes']/1024:.1f} KB in {res['elapsed_seconds']}s)")
            if res.get("title"):
                print(f"Title: {res['title']}")
            if res.get("visible_text_snippet"):
                print(f"Preview: {res['visible_text_snippet'][:200]}...")
        else:
            print(f"[FAIL] {res.get('error')} ({res.get('elapsed_seconds')}s)", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

