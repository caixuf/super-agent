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
    def cached(base):
        return [p for p in base.glob("chromium-*/chrome-linux64/chrome")
                if p.is_file() and os.access(p, os.X_OK)]

    def works(candidate):
        try:
            with tempfile.TemporaryDirectory(prefix="superagent-browser-probe-") as profile:
                proc = subprocess.run([str(candidate), "--headless", "--no-sandbox", "--disable-gpu",
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
    for name in ("google-chrome", "google-chrome-stable", "chromium"):
        binary = shutil.which(name)
        if binary and Path(binary).resolve().name != "snap":
            candidates.append(Path(binary))
    selected = next((path for path in candidates if works(path)), None)
    if selected is None:
        # Install into the managed deployment, never into an unrelated profile.
        env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(local_cache))
        packages = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                                   "playwright>=1.40"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if packages.returncode:
            raise RuntimeError("browser bootstrap dependency unavailable")
        browser = subprocess.run([str(python), "-m", "playwright", "install", "chromium"],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if browser.returncode:
            raise RuntimeError("Chromium download failed; check browser package network access")
        selected = next((path for path in cached(local_cache) if works(path)), None)
        if selected is None:
            raise RuntimeError("Chromium cannot start; check operating system browser dependencies")
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
