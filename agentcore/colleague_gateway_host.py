"""Optional host adapter for Hermes' ticket-authenticated Gateway connection.

The host pins profile homes; each gets a dedicated GatewayConnection. The process
peer identity is supplied by Hermes' private control channel, not caller/model data.
Gateway startup remains the host/operator's responsibility.
"""
from __future__ import annotations
from dataclasses import dataclass
import os
import re
import threading
from pathlib import Path

_PROFILE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")

@dataclass(frozen=True)
class _Profile:
    name: str
    home: Path
    principal: str

class ProfileBoundConnection:
    """One official GatewayConnection pinned to one host profile and peer identity."""
    def __init__(self, profile: _Profile, connection, gateway_profile_id: str):
        self.hermes_profile = profile.name
        self.gateway_profile_id = gateway_profile_id
        self.principal_id = profile.principal
        self._connection = connection
        self._closed = False

    def call(self, method, **params):
        if self._closed:
            raise RuntimeError("gateway_connection_closed")
        return self._connection.call(method, **params)

    def close(self):
        if not self._closed:
            self._closed = True
            self._connection.close()

class HermesGatewayHost:
    """Own per-profile official Gateway connections from trusted host config.

    ``profiles`` maps ``profile -> (HERMES_HOME, "process-peer")``. Hermes mints
    the ticket for the control socket's authenticated OS peer, and the stable
    principal is ``uid:<uid>`` on POSIX. User authorization remains a separate
    host responsibility. This adapter attaches only to an already-ready owner.
    """
    def __init__(self, profiles, *, connector=None):
        """``connector(profile, home) -> (connection, gateway_profile_id)``.

        Dialling lives with the host, not here: this package must not import the
        host's runtime modules. A host that supplies no connector simply cannot
        attach, which is visible instead of silently coupled.
        """
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("trusted_profile_map_required")
        parsed = {}
        for name, value in profiles.items():
            if (not isinstance(name, str) or not _PROFILE.fullmatch(name) or name in {"default", "latest"}
                    or not isinstance(value, (tuple, list)) or len(value) != 2):
                raise ValueError("invalid_trusted_profile")
            home, service_identity = value
            home = Path(home).expanduser().resolve(strict=True)
            if not home.is_dir() or service_identity != "process-peer":
                raise ValueError("invalid_trusted_profile")
            if os.name == "nt":
                raise RuntimeError("hermes_gateway_peer_principal_not_exposed_on_windows")
            if any(p.home == home for p in parsed.values()):
                raise ValueError("profile_home_reused")
            parsed[name] = _Profile(name, home, f"uid:{os.getuid()}")
        if connector is not None and not callable(connector):
            raise TypeError("gateway_connector_not_callable")
        self._connector = connector
        self._profiles = parsed
        self._connections = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def configured_profiles(self) -> frozenset[str]:
        """Profile names this host is trusted to attach; does not imply readiness."""
        return frozenset(self._profiles)

    def connection_for_profile(self, name: str) -> ProfileBoundConnection:
        with self._lock:
            if self._closed:
                raise RuntimeError("gateway_host_closed")
            profile = self._profiles.get(name)
            if profile is None:
                raise PermissionError("unconfigured_hermes_profile")
            current = self._connections.get(name)
            if current is not None and not current._closed:
                return current
            current = self._connect(profile)
            self._connections[name] = current
            return current

    def _connect(self, profile: _Profile) -> ProfileBoundConnection:
        if self._connector is None:
            raise RuntimeError("gateway_connector_required")
        connection, gateway_profile_id = self._connector(profile.name, profile.home)
        if not isinstance(gateway_profile_id, str) or not gateway_profile_id:
            raise ValueError("gateway_profile_identity_required")
        return ProfileBoundConnection(profile, connection, gateway_profile_id)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

def connection_for_profile(host: HermesGatewayHost, profile: str):
    """Convenience callback for ``GatewayColleagueDispatcher`` host injection."""
    if not isinstance(host, HermesGatewayHost):
        raise TypeError("HermesGatewayHost_required")
    return host.connection_for_profile(profile)
