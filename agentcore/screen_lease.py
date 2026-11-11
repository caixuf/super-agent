"""Bot Desktop takeover lease observer for Super Agent.

Reads the upstream Hermes Bot Desktop control lease file
(``<HERMES_HOME>/bot-desktop/lease.json``) without exposing capability tokens.

Security and determinism invariants:
- Pure Python standard library only (agentcore import-boundary gate).
- Missing ``lease.json`` means no active human takeover (``holder == "agent"``).
- Unreadable file, malformed JSON, or unknown ``holder`` fails closed to
  ``holder == "human"`` so neither agents nor timeout reapers race a human
  entering credentials on the shared screen.
- Raw ``viewer_id`` capability tokens are never stored or returned; only a
  12-hex-character SHA-256 prefix (``viewer_hash``) is exposed for diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

HUMAN_TAKEOVER_STAGE = "human_takeover_wait"
_VALID_HOLDERS = frozenset({"agent", "human"})


@dataclass(frozen=True)
class ScreenLeaseState:
    """Sanitized snapshot of ``<HERMES_HOME>/bot-desktop/lease.json``."""

    holder: str = "agent"
    viewer_hash: Optional[str] = None
    since: float = 0.0
    reason: str = ""
    epoch: int = 0
    lease_exists: bool = False
    fail_closed: bool = False

    @property
    def is_human_takeover(self) -> bool:
        return self.holder == "human"

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "holder": self.holder,
            "is_human_takeover": self.is_human_takeover,
            "viewer_hash": self.viewer_hash,
            "since": self.since,
            "reason": self.reason,
            "epoch": self.epoch,
            "lease_exists": self.lease_exists,
            "fail_closed": self.fail_closed,
        }


def lease_file_path(hermes_home: str | Path) -> Path:
    """Return canonical Bot Desktop lease path under *hermes_home*."""
    return Path(hermes_home).expanduser() / "bot-desktop" / "lease.json"


def _hash_viewer_id(viewer_id: Any) -> Optional[str]:
    if not isinstance(viewer_id, str) or not viewer_id:
        return None
    return hashlib.sha256(viewer_id.encode("utf-8")).hexdigest()[:12]


def read_bot_desktop_lease(hermes_home: str | Path | None) -> ScreenLeaseState:
    """Read and sanitize the Bot Desktop lease state for *hermes_home*.

    When *hermes_home* is ``None`` or empty, or ``lease.json`` does not exist,
    returns ``holder="agent"`` with ``lease_exists=False``.
    Any read/parse/schema error fails closed to ``holder="human"``.
    """
    if hermes_home is None or str(hermes_home).strip() == "":
        return ScreenLeaseState()

    path = lease_file_path(hermes_home)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ScreenLeaseState()
    except OSError:
        return ScreenLeaseState(
            holder="human",
            reason="lease_unreadable_fail_closed",
            lease_exists=True,
            fail_closed=True,
        )

    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        return ScreenLeaseState(
            holder="human",
            reason="lease_corrupt_json_fail_closed",
            lease_exists=True,
            fail_closed=True,
        )

    if not isinstance(data, dict):
        return ScreenLeaseState(
            holder="human",
            reason="lease_non_object_fail_closed",
            lease_exists=True,
            fail_closed=True,
        )

    holder = data.get("holder")
    if holder not in _VALID_HOLDERS:
        return ScreenLeaseState(
            holder="human",
            reason="lease_invalid_holder_fail_closed",
            lease_exists=True,
            fail_closed=True,
        )

    raw_since = data.get("since", 0.0)
    try:
        since = float(raw_since) if raw_since is not None else 0.0
    except (TypeError, ValueError):
        since = 0.0

    raw_epoch = data.get("epoch", 0)
    try:
        epoch = int(raw_epoch) if raw_epoch is not None else 0
    except (TypeError, ValueError):
        epoch = 0

    raw_reason = data.get("reason", "")
    reason = str(raw_reason) if isinstance(raw_reason, (str, int, float)) else ""

    return ScreenLeaseState(
        holder=holder,
        viewer_hash=_hash_viewer_id(data.get("viewer_id")),
        since=since,
        reason=reason,
        epoch=epoch,
        lease_exists=True,
        fail_closed=False,
    )


def is_human_takeover(hermes_home: str | Path | None) -> bool:
    """Return True when a human currently holds (or fail-closed owns) the screen."""
    return read_bot_desktop_lease(hermes_home).is_human_takeover


def resolve_job_hermes_home(
    payload: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    """Resolve target ``HERMES_HOME`` for a running pool job if configured.

    Checks job payload keys ``hermes_home`` / ``profile_home`` first, then
    environment variables ``HERMES_HOME`` / ``DEV_PROFILE_HOME``.
    Returns ``None`` when no Hermes profile home is configured.
    """
    if isinstance(payload, Mapping):
        for key in ("hermes_home", "profile_home"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return Path(val.strip()).expanduser()

    source_env = os.environ if env is None else env
    for key in ("HERMES_HOME", "DEV_PROFILE_HOME"):
        val = source_env.get(key)
        if isinstance(val, str) and val.strip():
            return Path(val.strip()).expanduser()

    return None
