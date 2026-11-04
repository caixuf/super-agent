#!/usr/bin/env python3
"""Set up direct completion delivery for a newly configured dispatch profile."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

import yaml
from configure_defaults import add_missing, load


def configure(config: Path, engine: Path):
    original = config.read_text(encoding="utf-8")
    load(original)
    data = yaml.safe_load(original) or {}
    settings = (((data.get("plugins") or {}).get("entries") or {}).get("durable-dispatch") or {}).get("settings") or {}
    if settings.get("enabled") is not True:
        print("dispatch disabled; completion defaults unchanged")
        return
    # Preserve all explicit delivery choices; never silently change old delivery
    # policy or introduce a cutoff into an existing enabled poller.
    if "deliver_completions" in settings:
        print("existing completion delivery policy preserved")
        return
    if settings.get("direct_delivery_enabled") is False:
        print("direct delivery explicitly disabled; completion defaults unchanged")
        return
    if "delivery_not_before" in settings:
        raise ValueError("existing delivery cutoff requires explicit completion policy")
    engine_setting = settings.get("engine_root", str(engine))
    if not isinstance(engine_setting, str) or not Path(engine_setting).is_absolute():
        raise ValueError("absolute engine root required")
    transport = Path(engine_setting).resolve() / "runtime/hermes_completion_transport.py"
    if not transport.is_file():
        raise ValueError("installed completion transport missing")
    if settings.get("delivery_transport_file") not in (None, str(transport)):
        raise ValueError("existing completion transport differs; explicit configuration required")
    defaults = {"direct_delivery_enabled": True, "delivery_transport_file": str(transport),
                "delivery_not_before": time.time(), "deliver_completions": True}
    updated = original
    for key, value in defaults.items():
        updated = add_missing(updated, ["plugins", "entries", "durable-dispatch", "settings", key], value)
    if updated == original:
        return
    fd, tmp = tempfile.mkstemp(prefix=".delivery-defaults-", dir=config.parent)
    try:
        os.fchmod(fd, config.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(updated)
            out.flush()
            os.fsync(out.fileno())
        shutil.copy2(config, config.with_name(config.name + ".before-delivery-defaults"))
        os.replace(tmp, config)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("configured direct completion delivery for new tasks; gateway reload required")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("usage: configure_delivery.py CONFIG ENGINE")
        configure(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit("completion setup failed: " + str(exc))
