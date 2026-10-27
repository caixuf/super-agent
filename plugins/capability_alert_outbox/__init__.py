"""Thin integration entry for an existing capability inspection routine.

The caller supplies the already-computed inspector result. This package never
collects a baseline, inspects host capabilities, repairs configuration, or
contacts a messaging platform.
"""
from __future__ import annotations

from typing import Any

from agentcore.ops_alerts import AlertOutbox, Sender, deliver_once


def create_outbox(queue_root: str) -> AlertOutbox:
    """Bind to the shared durable database and create only the C-owned tables."""
    return AlertOutbox(queue_root)


def record_inspection(outbox: AlertOutbox, *, profile: str, scope: str,
                      inspection: dict[str, Any], config_persisted: bool,
                      runtime_loaded: bool) -> dict[str, Any]:
    """Pass through one result from the caller's authoritative inspector."""
    return outbox.observe(profile, scope, inspection,
                          config_persisted=config_persisted,
                          runtime_loaded=runtime_loaded)


def deliver_pending(outbox: AlertOutbox, sender: Sender | None) -> dict[str, Any]:
    """Deliver one event, or report unavailable without leasing when unconfigured."""
    return deliver_once(outbox, sender)
