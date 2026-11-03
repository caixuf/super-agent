"""Host-controlled, tool-bearing Hermes Gateway colleague backend.

This attaches pre-existing, channel-free profiles to the live Gateway dispatcher
with full toolsets, verified private workspaces, and authoritative quiescence attestation.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from types import MappingProxyType


class ToolEnabledGatewayProfileBackend:
    """Bind existing members to private profiles under a full tool policy."""

    def __init__(self, *, gateway_host, member_profiles, profile_homes, model_options, profile_workdirs=None):
        if not member_profiles or not isinstance(member_profiles, dict):
            raise ValueError("member_profile_map_required")
        self.host = gateway_host
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
            if set(values) != {"model", "provider", "base_url", "api_key"} or any(
                    not isinstance(v, str) or not v.strip() for v in values.values()):
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
        if state.get("state") != "ready":
            raise RuntimeError("member_gateway_not_ready")

    def inspect(self, spec):
        try:
            profile = self._profile(spec)
            home = self.homes[profile]
            self._private_dir(home)
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
            return {"state": "ready", "isolated": True, "private_memory": True, "no_channels": True}
        except Exception:
            return {"state": "unknown"}

    def idle(self, spec):
        try:
            profile = self._profile(spec)
            connection = self.host.connection_for_profile(profile)
            active = connection.call("session.active_list")
            if not isinstance(active, dict) or not isinstance(active.get("sessions"), list):
                return False
            return all(s.get("status") == "idle" for s in active["sessions"])
        except Exception:
            return False

    def park(self, spec):
        if not self.idle(spec):
            raise RuntimeError("cannot_park_busy_member")
