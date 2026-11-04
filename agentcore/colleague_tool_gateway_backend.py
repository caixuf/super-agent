"""Host-controlled, tool-bearing Hermes Gateway colleague backend.

This can attach pre-existing channel-free profiles to the Gateway. Private
workspaces are not execution isolation. A live profile can accept a turn;
whole-attempt termination remains a separate, explicitly unknown observation.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from types import MappingProxyType


class ToolEnabledGatewayProfileBackend:
    """Bind existing members to private profiles under a full tool policy."""

    def __init__(self, *, gateway_host, member_profiles, profile_homes, model_options, profile_workdirs=None,
                 idle_authority=None, stop_profile=None, parked_profile=None, start_profile=None):
        if not member_profiles or not isinstance(member_profiles, dict):
            raise ValueError("member_profile_map_required")
        if idle_authority is not None and not callable(idle_authority):
            raise ValueError("invalid_idle_authority")
        if stop_profile is not None and not callable(stop_profile):
            raise ValueError("invalid_profile_lifecycle")
        if parked_profile is not None and not callable(parked_profile):
            raise ValueError("invalid_parked_observer")
        if start_profile is not None and not callable(start_profile):
            raise ValueError("invalid_profile_start")
        self.host = gateway_host
        self._start_profile = start_profile
        self._idle_authority = idle_authority
        self._stop_profile = stop_profile
        self._parked_profile = parked_profile
        self.members = MappingProxyType(dict(member_profiles))
        self.homes = {}
        self.workdirs = {}
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
            # Empty options intentionally follow the member's Hermes profile.
            # A nonempty override must remain complete and host supplied.
            if values and (set(values) != {"model", "provider", "base_url", "api_key"} or any(
                    not isinstance(v, str) or not v.strip() for v in values.values())):
                raise ValueError("explicit_model_options_required")
            self.homes[profile] = home
            if profile_workdirs and profile in profile_workdirs:
                wdir = Path(profile_workdirs[profile]).resolve(strict=True)
                self._private_dir(wdir)
                self.workdirs[profile] = wdir
            else:
                self.workdirs[profile] = home
            # safe_mode=False enables all tools, skills, and plugins for this colleague
            self._options[profile] = MappingProxyType({"safe_mode": False, **values})

    @staticmethod
    def _private_dir(path):
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("private_directory_required")

    @staticmethod
    def _assert_no_channels(home):
        try:
            from ruamel.yaml import YAML
            config = YAML(typ="safe").load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except ModuleNotFoundError:
            import yaml
            config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            raise ValueError("profile_config_invalid")
        channels = config.get("platforms") or {}
        if not isinstance(channels, dict) or any(
                not isinstance(v, dict) or v.get("enabled") is True for v in channels.values()):
            raise ValueError("messaging_channels_not_disabled")
        env = home / ".env"
        if env.exists():
            from dotenv import dotenv_values
            names = dotenv_values(env)
            prefixes = ("FEISHU_", "LARK_", "TELEGRAM_", "DISCORD_", "SLACK_", "WHATSAPP_")
            if any(k.upper().startswith(prefixes) and v for k, v in names.items()):
                raise ValueError("messaging_credentials_not_allowed")

    def session_options(self, profile):
        return dict(self._options[profile])

    def _profile(self, spec):
        profile = self.members.get((spec.member_id, spec.generation))
        if profile is None:
            raise ValueError("member_profile_binding_missing")
        return profile

    def ensure(self, spec):
        state = self.inspect(spec)
        if state.get("state") == "parked" and self._start_profile is not None:
            profile = self._profile(spec)
            self._start_profile(profile, self.homes[profile])
            state = self.inspect(spec)
        if state.get("state") != "available":
            raise RuntimeError("member_gateway_not_ready")

    def inspect(self, spec):
        try:
            profile = self._profile(spec)
            home = self.homes[profile]
            self._private_dir(home)
            if self._parked_profile is not None and self._parked_profile(profile, home) is True:
                return {"state": "parked"}
            self._private_dir(home / "memories")
            if profile in self.workdirs:
                self._private_dir(self.workdirs[profile])
            self._assert_no_channels(home)
            connection = self.host.connection_for_profile(profile)
            if connection.gateway_profile_id != str(home) or connection.hermes_profile != profile:
                return {"state": "unknown"}
            pong = connection.call("ping")
            if not isinstance(pong, dict) or pong.get("pong") is not True:
                return {"state": "unknown"}
            # Reachability is not a sandbox or a whole-attempt stop proof.
            return {"state": "available", "memory_directory": "owner_only", "no_channels": True,
                    "execution_isolation": "unverified"}
        except Exception:
            return {"state": "unknown"}

    def idle(self, spec):
        # Gateway session idleness cannot establish whole-work quiescence.
        if self._idle_authority is None:
            return False
        try:
            return self._idle_authority(spec) is True
        except Exception:
            return False

    def park(self, spec):
        if not self.idle(spec):
            raise RuntimeError("authoritative_quiescence_required")
        if self._stop_profile is None:
            raise RuntimeError("member_gateway_stop_not_implemented")
        profile = self._profile(spec)
        self._stop_profile(profile, self.homes[profile])
