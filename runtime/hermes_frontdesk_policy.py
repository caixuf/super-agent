"""Host-side, profile-scoped frontdesk authorization; not a model-callable tool.

Installed as ``superagent_frontdesk.py`` by the explicit host patch. Configuration
lives in the owning profile's config.yaml, never in tool arguments or a new env
flag. A valid profile without this opt-in retains its existing behavior.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any

TASK_TOOLS = frozenset({"durable_submit", "durable_status", "durable_cancel", "task_status"})
MEMORY_READ_TOOLS = frozenset({
    "memory_search_l1", "memory_timeline_l2", "memory_get_observations_l3",
})
CHAT_COMMANDS = frozenset({"help", "commands", "whoami", "version", "new", "reset", "stop", "status"})
UNAVAILABLE = "frontdesk_policy_unavailable: execution refused; ask the administrator to check this profile."
TOOL_DENIED = "frontdesk_tool_denied: use the durable task APIs; do not execute or delegate work in this profile."
COMMAND_DENIED = "frontdesk_command_denied: this profile only supports chat controls and durable task operations."
_MAX_CONFIG_BYTES = 1024 * 1024


def _settings(home: Path | str | None = None) -> dict[str, bool]:
    if home is None:
        from agent.secret_scope import is_multiplex_active
        from hermes_constants import get_hermes_home, get_hermes_home_override
        # A worker that lost its runtime scope must not borrow the launch
        # profile's broader permissions in a multiplexed process.
        if is_multiplex_active() and not get_hermes_home_override():
            raise ValueError("frontdesk profile scope missing")
        home = get_hermes_home()
    path = Path(home) / "config.yaml"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"enabled": False, "memory_read_enabled": False}
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("profile config must be a regular file")
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("profile config exceeds budget")

    # Match the host's hermes_yaml policy, including YAML 1.1 booleans and
    # duplicate-key rejection. Parsers are per call, never shared by threads.
    try:
        from ruamel.yaml import YAML
        loader = YAML(typ="safe", pure=True)
        loader.version = (1, 1)
        loader.allow_duplicate_keys = False
        config = loader.load(raw)
    except ImportError:
        import yaml
        config = yaml.safe_load(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("profile config must be a mapping")
    section = config.get("superagent", {})
    if not isinstance(section, dict):
        raise ValueError("superagent settings must be a mapping")
    policy = section.get("frontdesk", {})
    if not isinstance(policy, dict) or set(policy) - {"enabled", "memory_read_enabled"}:
        raise ValueError("invalid frontdesk settings")
    enabled = policy.get("enabled", False)
    memory_read = policy.get("memory_read_enabled", False)
    if type(enabled) is not bool or type(memory_read) is not bool:
        raise ValueError("frontdesk flags must be boolean")
    return {"enabled": enabled, "memory_read_enabled": memory_read}


def tool_block_message(tool_name: str, *, home: Path | str | None = None) -> str | None:
    try:
        policy = _settings(home)
        if not policy["enabled"]:
            return None
        allowed = TASK_TOOLS | (MEMORY_READ_TOOLS if policy["memory_read_enabled"] else frozenset())
        return None if isinstance(tool_name, str) and tool_name in allowed else TOOL_DENIED
    except Exception:
        # Never copy config contents, paths or parser exception bodies into IM.
        return UNAVAILABLE


def frontdesk_enabled(*, home: Path | str | None = None) -> bool:
    """Trusted host mode query; malformed/missing scope errors are not opt-out."""
    return _settings(home)["enabled"]


def filter_tool_definitions(
    definitions: list[dict[str, Any]], *, home: Path | str | None = None,
) -> list[dict[str, Any]]:
    try:
        policy = _settings(home)
        if not policy["enabled"]:
            return definitions
        allowed = TASK_TOOLS | (MEMORY_READ_TOOLS if policy["memory_read_enabled"] else frozenset())
        return [item for item in definitions if item.get("function", {}).get("name") in allowed]
    except Exception:
        return []


def command_block_message(
    command: str | None, *, home: Path | str | None = None,
) -> str | None:
    if command is None:
        return None
    try:
        policy = _settings(home)
        if not policy["enabled"]:
            return None
        # Unknown commands, aliases and skill/exec quick commands are not an
        # alternate authority. Do not expand them before authorization.
        return None if isinstance(command, str) and command in CHAT_COMMANDS else COMMAND_DENIED
    except Exception:
        return UNAVAILABLE
