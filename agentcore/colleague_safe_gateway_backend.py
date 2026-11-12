"""Host-controlled, tool-free Hermes Gateway colleague backend.

This is a read-only model-session boundary, not an OS sandbox. It can attach
pre-existing, channel-free profiles; it neither creates nor stops gateways.
It cannot attest external work quiescence or authorize tool-bearing tasks.
"""
from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from agentcore.private_profile import assert_no_messaging_channels, require_private_directory


class SafeGatewayProfileBackend:
    """Bind existing members to private profiles under an immutable safe policy."""

    def __init__(self, *, gateway_host, member_profiles, profile_homes, model_options):
        if not member_profiles or not isinstance(member_profiles, dict):
            raise ValueError("member_profile_map_required")
        self.host = gateway_host
        self.members = MappingProxyType(dict(member_profiles))
        self.homes = {}
        self._options = {}
        identities = set()
        for profile in set(self.members.values()):
            raw = Path(profile_homes[profile])
            if not raw.is_absolute() or raw.is_symlink():
                raise ValueError("private_profile_home_required")
            home = raw.resolve(strict=True)
            if home in identities or profile not in gateway_host.configured_profiles:
                raise ValueError("unique_bound_profile_required")
            identities.add(home)
            self._private_dir(home)
            memory = home / "memories"
            if memory.is_symlink():
                raise ValueError("private_memory_required")
            self._private_dir(memory)
            self._assert_no_channels(home)
            values = dict(model_options[profile])
            if set(values) != {"model", "provider", "base_url", "api_key"} or any(
                    not isinstance(v, str) or not v.strip() for v in values.values()):
                raise ValueError("explicit_model_options_required")
            self.homes[profile] = home
            self._options[profile] = MappingProxyType({"safe_mode": True, **values})

    @staticmethod
    def _private_dir(path):
        require_private_directory(path, "private_directory_required", follow_symlinks=True)

    @staticmethod
    def _assert_no_channels(home):
        assert_no_messaging_channels(home)

    def session_options(self, profile):
        return dict(self._options[profile])

    def _profile(self, spec):
        profile = self.members.get((spec.member_id, spec.generation))
        if profile is None:
            raise ValueError("member_profile_binding_missing")
        return profile

    def ensure(self, spec):
        state = self.inspect(spec)
        if state.get("state") != "ready":
            raise RuntimeError("member_gateway_not_ready")

    def inspect(self, spec):
        try:
            profile = self._profile(spec)
            home = self.homes[profile]
            self._private_dir(home)
            self._private_dir(home / "memories")
            self._assert_no_channels(home)
            connection = self.host.connection_for_profile(profile)
            if connection.gateway_profile_id != str(home) or connection.hermes_profile != profile:
                return {"state": "unknown"}
            pong = connection.call("ping")
            if not isinstance(pong, dict) or pong.get("pong") is not True:
                return {"state": "unknown"}
            return {"state": "ready", "isolated": True, "private_memory": True, "no_channels": True}
        except Exception:
            return {"state": "unknown"}

    def idle(self, spec):
        # A gateway connection is not proof that all descendants and delivery are idle.
        return False

    def park(self, spec):
        raise RuntimeError("authoritative_quiescence_required")
