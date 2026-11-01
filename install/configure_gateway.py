#!/usr/bin/env python3
"""Mark a new Super Agent profile as standalone before installing its gateway."""
from pathlib import Path
import os
import shutil
import sys
import tempfile

import yaml
from configure_defaults import add_missing, load


def configure(path):
    original = path.read_text(encoding="utf-8") if path.exists() else "{}\n"
    data = yaml.safe_load(original) or {}
    gateway = data.get("gateway") or {}
    if not isinstance(gateway, dict):
        raise ValueError("gateway configuration must be a mapping")
    if "standalone" in gateway and gateway["standalone"] is not True:
        raise ValueError("existing gateway.standalone choice conflicts with standalone service")
    if gateway.get("standalone") is True:
        return
    if original.strip() == "{}":
        updated = "gateway:\n  standalone: true\n"
    else:
        load(original)
        updated = add_missing(original, ["gateway", "standalone"], True)
    fd, temporary = tempfile.mkstemp(prefix=".gateway-defaults-", dir=path.parent)
    try:
        os.fchmod(fd, (path.stat().st_mode & 0o777) if path.exists() else 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".before-gateway-defaults"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    try:
        configure(Path(sys.argv[1]))
    except (IndexError, OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit("gateway setup failed: " + str(exc))
