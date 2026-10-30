"""Thin integration entry for an existing capability inspection routine.

The caller supplies the already-computed inspector result. This package never collects a
baseline, inspects host capabilities, repairs configuration, or contacts a messaging
platform.

The heavy ``agentcore`` package is not importable from a Hermes runtime that installs this
plugin by copy, so the entry loads it from the administrator-configured checkout the same
way the other plugins do — at call time, not at import time. The checkout is taken
from ``plugins.entries.capability-alert-outbox.settings.engine_root``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


_ENGINE = {"root": ""}


def _alerts():
    """(AlertOutbox, deliver_once); the checkout is the one given by ``register(ctx)``."""
    try:
        from agentcore.ops_alerts import AlertOutbox, deliver_once
        return AlertOutbox, deliver_once
    except ModuleNotFoundError as error:
        if error.name != "agentcore":
            raise
    root = _ENGINE["root"]
    if not root:
        raise RuntimeError("agentcore is not importable; the plugin needs engine_root in its settings")
    source = Path(root).resolve(strict=True) / "agentcore/ops_alerts.py"
    spec = importlib.util.spec_from_file_location("capability_alert_outbox_engine", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AlertOutbox, module.deliver_once


def create_outbox(queue_root: str) -> Any:
    """Bind to the shared durable database and create only the C-owned tables."""
    return _alerts()[0](queue_root)


def record_inspection(outbox: Any, *, profile: str, scope: str,
                      inspection: dict[str, Any], config_persisted: bool,
                      runtime_loaded: bool) -> dict[str, Any]:
    """Pass through one result from the caller's authoritative inspector."""
    return outbox.observe(profile, scope, inspection,
                          config_persisted=config_persisted,
                          runtime_loaded=runtime_loaded)


def deliver_pending(outbox: Any, sender: Any) -> dict[str, Any]:
    """Deliver one event, or report unavailable without leasing when unconfigured."""
    return _alerts()[1](outbox, sender)


def register(ctx):  # pragma: no cover - no hooks: this entry only exposes helpers
    """No tools or hooks to wire; remember the engine checkout for the helpers above."""
    root = ctx.get_config("engine_root", "")
    if isinstance(root, str) and Path(root).is_absolute():
        _ENGINE["root"] = root
