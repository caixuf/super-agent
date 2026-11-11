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

import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

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


def _open_private(path: str | bytes | os.PathLike[str], flags: int) -> int:
    return os.open(path, flags, 0o600)


@contextlib.contextmanager
def _lease_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    if fcntl is None:
        yield
        return
    lock_file = path.with_suffix(".lock")
    fh = open(lock_file, "a+", encoding="utf-8", opener=_open_private)
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _read_raw_lease(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"holder": "agent", "viewer_id": None, "since": time.time(), "reason": "", "epoch": 0}
    except OSError:
        return {
            "holder": "human",
            "viewer_id": "unreadable-lease",
            "since": time.time(),
            "reason": "lease file unreadable",
            "epoch": 0,
        }
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("holder") not in _VALID_HOLDERS:
        return {
            "holder": "human",
            "viewer_id": "unreadable-lease",
            "since": time.time(),
            "reason": "lease file corrupt",
            "epoch": 0,
        }
    return {
        "holder": data.get("holder", "agent"),
        "viewer_id": data.get("viewer_id"),
        "since": float(data.get("since") or 0.0),
        "reason": str(data.get("reason") or ""),
        "epoch": int(data.get("epoch") or 0),
    }


def _write_raw_lease(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", opener=_open_private) as fh:
        fh.write(json.dumps(payload))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def viewer_holds_lease(hermes_home: str | Path | None, viewer_id: str) -> bool:
    """Return True only when *viewer_id* is non-empty and actively holds the human lease."""
    if not viewer_id or hermes_home is None or not str(hermes_home).strip():
        return False
    raw = _read_raw_lease(lease_file_path(hermes_home))
    return raw.get("holder") == "human" and raw.get("viewer_id") == viewer_id


def acquire_bot_desktop_lease(
    hermes_home: str | Path,
    viewer_id: str,
    *,
    reason: str = "",
) -> ScreenLeaseState:
    """Atomically acquire human screen control under upstream-compatible ``lease.lock``."""
    if not viewer_id or not viewer_id.strip():
        raise ValueError("viewer_id must be a non-empty string")
    path = lease_file_path(hermes_home)
    with _lease_lock(path):
        cur = _read_raw_lease(path)
        if not (cur["holder"] == "human" and cur["viewer_id"] == viewer_id):
            cur["holder"] = "human"
            cur["viewer_id"] = viewer_id
            cur["since"] = time.time()
            cur["reason"] = reason or ""
            cur["epoch"] = int(cur.get("epoch") or 0) + 1
            _write_raw_lease(path, cur)
    return read_bot_desktop_lease(hermes_home)


def release_bot_desktop_lease(
    hermes_home: str | Path,
    viewer_id: Optional[str] = None,
    *,
    force: bool = False,
) -> ScreenLeaseState:
    """Return screen control to the agent (honoring *viewer_id* unless *force* is True)."""
    path = lease_file_path(hermes_home)
    with _lease_lock(path):
        cur = _read_raw_lease(path)
        if cur["holder"] != "agent":
            if force or viewer_id is None or cur.get("viewer_id") == viewer_id:
                cur["holder"] = "agent"
                cur["viewer_id"] = None
                cur["since"] = time.time()
                cur["reason"] = ""
                cur["epoch"] = int(cur.get("epoch") or 0) + 1
                _write_raw_lease(path, cur)
    return read_bot_desktop_lease(hermes_home)


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

