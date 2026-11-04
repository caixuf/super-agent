"""Host-side, profile-scoped frontdesk authorization; not a model-callable tool.

Installed as ``superagent_frontdesk.py`` by the explicit host patch. Configuration
lives in the owning profile's config.yaml, never in tool arguments or a new env
flag. A valid profile without this opt-in retains its existing behavior.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Any

# Read-only roster queries belong to the thin frontdesk: they have no source files,
# so forcing them through durable_submit (which requires sources inside the
# administrator roots) makes every such question fail closed. Keep this set free of
# anything that executes, writes, provisions or delegates work.
TASK_TOOLS = frozenset({"durable_submit", "durable_status", "durable_cancel",
                        "durable_roster", "task_status", "colleague_list"})
MEMORY_READ_TOOLS = frozenset({
    "memory_search_l1", "memory_timeline_l2", "memory_get_observations_l3",
})
CHAT_COMMANDS = frozenset({"help", "commands", "whoami", "version", "new", "reset", "stop", "status"})
UNAVAILABLE = "frontdesk_policy_unavailable: execution refused; ask the administrator to check this profile."
TOOL_DENIED = "frontdesk_tool_denied: use the durable task APIs; do not execute or delegate work in this profile."
COMMAND_DENIED = "frontdesk_command_denied: this profile only supports chat controls and durable task operations."
_MAX_CONFIG_BYTES = 1024 * 1024

#: Programmatic backend entries (the relay bridge's /v1/runs, API-server clients)
#: are not a human frontdesk. Restricting them to task APIs would break every
#: ordinary question that needs a real tool, so they keep their full toolset.
#: Only messaging surfaces get the thin-frontdesk restriction; an unknown or
#: unreadable platform stays restricted (fail-closed).
_BACKEND_PLATFORMS = frozenset({"api_server", "local"})


def _backend_session() -> bool:
    platform = ""
    try:
        from gateway.session_context import get_session_env
        platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip()
    except Exception:
        # Host session context is unavailable (isolated runner, test harness):
        # fall back to the raw env var the host binds. Unset stays restricted.
        platform = (os.environ.get("HERMES_SESSION_PLATFORM", "") or "").strip()
    return platform in _BACKEND_PLATFORMS


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
    except ImportError:
        venv_lib = Path.home() / ".hermes/hermes-agent/venv/lib"
        if venv_lib.is_dir():
            for sp in venv_lib.glob("python*/site-packages"):
                if sp.is_dir() and str(sp) not in sys.path:
                    sys.path.append(str(sp))
        try:
            from ruamel.yaml import YAML
        except ImportError:
            YAML = None

    if YAML is not None:
        loader = YAML(typ="safe", pure=True)
        loader.version = (1, 1)
        loader.allow_duplicate_keys = False
        config = loader.load(raw)
    else:
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
        if not policy["enabled"] or _backend_session():
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
        if not policy["enabled"] or _backend_session():
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
