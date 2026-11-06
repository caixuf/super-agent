#!/usr/bin/env python3
"""Prepare a private Python runtime for Super Agent installation and document rendering."""
import argparse
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

MODULES = ("yaml", "markdown", "pygments", "PIL", "websocket")
REQUIREMENTS = ("PyYAML>=6.0", "Markdown>=3.4", "Pygments>=2.15", "Pillow>=10", "websocket-client>=1.6")


def ready(python: Path) -> bool:
    if not python.is_file():
        return False
    check = subprocess.run([str(python), "-c", ";".join("import " + name for name in MODULES)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return check.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine_root", type=Path)
    args = parser.parse_args()
    root = args.engine_root.resolve()
    python = root / ".superagent-venv/bin/python"
    if not python.is_file():
        root.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(root / ".superagent-venv")
    if not ready(python):
        result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                 *REQUIREMENTS], check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        if result.returncode or not ready(python):
            raise RuntimeError("private document dependencies unavailable; check Python venv and package network access")
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright")))
    local_cache = root / ".browser-cache"
    _SHELL_PATTERNS = (
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-linux64/chrome-headless-shell",
        "chromium-*/chrome-linux64/chrome-headless-shell",
        "chromium-*/chrome-linux/chrome-headless-shell",
        "chrome-headless-shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
    )
    def cached(base):
        res = []
        for pat in _SHELL_PATTERNS:
            res.extend(p for p in base.glob(pat) if p.is_file() and os.access(p, os.X_OK))
        return res

    def works(candidate):
        try:
            with tempfile.TemporaryDirectory(prefix="superagent-browser-probe-") as profile:
                proc = subprocess.run([str(candidate), "--headless=new", "--no-sandbox", "--disable-gpu",
                                       "--disable-dev-shm-usage", "--user-data-dir=" + profile,
                                       "--dump-dom", "data:text/html,<p>probe</p>"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                return proc.returncode == 0 and b"probe" in proc.stdout
        except (OSError, subprocess.TimeoutExpired):
            return False

    candidates = []
    configured = os.environ.get("ARCH_DOC_BROWSER")
    if configured:
        configured_path = shutil.which(configured)
        if not configured_path or not works(Path(configured_path)):
            raise RuntimeError("configured ARCH_DOC_BROWSER cannot start")
        candidates.append(Path(configured_path))
    candidates += cached(cache) + cached(local_cache)
    for name in ("chrome-headless-shell", "chromium-headless-shell", "google-chrome", "google-chrome-stable", "chromium"):
        binary = shutil.which(name)
        if binary and Path(binary).resolve().name != "snap":
            candidates.append(Path(binary))
    selected = next((path for path in candidates if works(path)), None)
    if selected is None:
        # 1. Try Playwright chromium-headless-shell or chromium
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(local_cache))
        packages = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                   "playwright>=1.40"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if packages.returncode == 0:
            for target in ("chromium-headless-shell", "chromium"):
                subprocess.run([str(python), "-m", "playwright", "install", target],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                selected = next((path for path in cached(local_cache) if works(path)), None)
                if selected:
                    break

        # 2. Standalone fallback: direct download via tools.doc_browser.obtain_headless_shell
        if selected is None:
            try:
                sys.path.insert(0, str(root))
                from tools.doc_browser import obtain_headless_shell
                obtained = obtain_headless_shell(local_cache)
                if obtained and works(Path(obtained)):
                    selected = Path(obtained)
            except Exception:
                pass

        if selected is None:
            raise RuntimeError("Chromium / chrome-headless-shell cannot start; check operating system browser dependencies")
    marker = root / ".browser-path"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".browser-path-", delete=False) as out:
        out.write(str(selected.resolve()) + "\n")
        temporary = Path(out.name)
    try:
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
    print("private document runtime and browser ready")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        sys.exit("dependency setup failed: " + str(exc))
