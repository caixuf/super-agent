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

# Read-only roster queries belong to the thin frontdesk: they have no source files,
# so forcing them through durable_submit (which requires sources inside the
# administrator roots) makes every such question fail closed. Keep this set free of
# anything that executes, writes, provisions or delegates work.
TASK_TOOLS = frozenset({
    "durable_submit", "durable_status", "durable_cancel",
    "durable_roster", "task_status", "colleague_list",
    "lan_node_list", "lan_node_probe",
    "skills_list", "skill_view",
    "feishu_doc_read", "web_extract", "web_search", "vision_analyze",
})
MEMORY_READ_TOOLS = frozenset({
    "memory_search_l1", "memory_timeline_l2", "memory_get_observations_l3",
})
# Opt-in evidence tier (``read_tools_enabled``): local file reads, session search
# and the read-only half of the host MCP server. A frontdesk that cannot look
# anything up either guesses or pushes every question to a backend colleague;
# this tier lets it ground answers in facts. Nothing here writes, submits test
# tasks, cancels work or runs a long LLM pipeline.
_IMFL_MCP_READ = (
    "imfl_route", "imfl_health", "imfl_list_tasks", "imfl_inspect_task",
    "imfl_task_detail", "imfl_task_progress", "imfl_task_report",
    "imfl_compare_reports", "imfl_query_manifest", "imfl_case_ast",
    "imfl_feishu_status", "imfl_list_groups", "imfl_list_cases", "imfl_get_case",
    "imfl_list_matchers", "imfl_state_registry", "imfl_recall_memory",
    "imfl_memory_status", "imfl_recall_experience", "imfl_locate_artifact",
    "imfl_case_health", "imfl_search_logs", "imfl_list_tool_gaps",
    "imfl_query_knowledge", "imfl_knowledge_status", "dsbot_colleague_result",
)
READ_TOOLS = frozenset({
    "read_file", "search_files", "session_search", "todo", "clarify",
    *(f"mcp__imfl__{name}" for name in _IMFL_MCP_READ),
})
# Opt-in shell tier (``shell_enabled``): the operator explicitly trusts this
# frontdesk with a host shell. Command scope is governed by the identity prompt,
# not by this gate, so it is off unless the profile config turns it on.
SHELL_TOOLS = frozenset({"terminal", "process"})
# Opt-in HR / team management tier (``hr_enabled``): allows the frontdesk to
# define roles, hire members, delegate management, and retire members.
HR_TOOLS = frozenset({
    "colleague_define_role", "colleague_hire", "colleague_grant", "colleague_retire",
    "colleague_catalog",
})
_FLAG_NAMES = ("enabled", "memory_read_enabled", "read_tools_enabled", "shell_enabled", "hr_enabled")
CHAT_COMMANDS = frozenset({"help", "commands", "whoami", "version", "new", "reset", "stop", "status", "boost"})
UNAVAILABLE = "frontdesk_policy_unavailable: execution refused; ask the administrator to check this profile."
TOOL_DENIED = "frontdesk_tool_denied: use the durable task APIs; do not execute or delegate work in this profile."
COMMAND_DENIED = "frontdesk_command_denied: this profile only supports chat controls and durable task operations."
_MAX_CONFIG_BYTES = 1024 * 1024

#: YAML 1.1 booleans, exactly as the host's ruamel resolver reads them.
_YAML11_TRUE = frozenset({"y", "yes", "on", "true"})
_YAML11_FALSE = frozenset({"n", "no", "off", "false"})


def _scalar(text: str) -> Any:
    """One plain or quoted scalar, with YAML 1.1 booleans."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if text.startswith("[") or text.startswith("{"):
        raise ValueError("frontdesk policy uses an unsupported value")
    low = text.lower()
    if low in _YAML11_TRUE:
        return True
    if low in _YAML11_FALSE:
        return False
    if low in ("", "null", "~"):
        return None
    return text


def _parse_mapping(text: str) -> Any:
    """Parse the indentation-based mapping subset the policy reads.

    Strict on purpose: a duplicate key is an error, not a last-one-wins, and
    a construct this parser does not understand is an error too. A policy that
    cannot be read has to fail closed.
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        if ":" not in body:
            raise ValueError("frontdesk policy is not a mapping")
        key, _, rest = body.partition(":")
        key = key.strip()
        if not key or not key.replace("-", "").replace("_", "").isalnum():
            raise ValueError("frontdesk policy has an unsupported key")
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise ValueError("frontdesk policy has a duplicate key")
        rest = rest.strip()
        if not rest:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(rest)
    return root


def _parse_frontdesk_block(raw: bytes) -> Any:
    """Parse only the ``superagent:`` block, with the host's semantics.

    The policy reads one small section, so the fallback reads that section
    alone: the rest of the profile config — lists, long quoted documents, the
    plugin tree — is none of this module's business, and a narrow parser that
    refuses what it does not understand fails closed instead of guessing.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    block: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if not inside:
            if not stripped or stripped.startswith("#") or line.startswith(" "):
                continue
            key, _, rest = stripped.partition(":")
            if key.strip() != "superagent":
                continue
            if rest.strip():
                # The section is an inline scalar, not a block.
                return _scalar(rest)
            inside = True
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith(" "):
            break
        block.append(line)
    if not block:
        return {}
    return _parse_mapping("superagent:\n" + "\n".join(block))


def _load_config(raw: bytes) -> Any:
    """Parse the profile config the way the host parses it.

    The host exposes ``hermes_yaml``: ruamel in YAML 1.1 mode, which reads
    bare ``y``/``n`` booleans and rejects duplicate keys. The fallbacks below
    reproduce those two rules rather than approximating them — a policy read
    under different rules than the host uses is not the host's policy:

    1. the host's own loader;
    2. ruamel with the same settings;
    3. PyYAML, corrected to the same two rules (its resolver drops bare
       ``y``/``n``, and its loader keeps duplicate keys);
    4. a narrow local parser for the block subset, when no YAML library is
       importable at all.
    """
    try:
        from hermes_yaml import safe_load
    except ImportError:
        pass
    else:
        return safe_load(raw)
    try:
        from ruamel.yaml import YAML
    except ImportError:
        pass
    else:
        loader = YAML(typ="safe", pure=True)
        loader.version = (1, 1)
        loader.allow_duplicate_keys = False
        return loader.load(raw)
    try:
        return _pyyaml_load(raw)
    except ImportError:
        return _parse_frontdesk_block(raw)


def _pyyaml_load(raw: bytes) -> Any:
    """PyYAML, corrected to the host's YAML 1.1 policy.

    Two corrections, both required to agree with the host: bare ``y``/``n``
    count as booleans, and a duplicate key is an error rather than a
    last-one-wins. The corrections are scoped to a private loader class, so
    PyYAML's own behaviour is untouched for every other caller.
    """
    import re

    import yaml

    class _Host11Loader(yaml.SafeLoader):
        pass

    _Host11Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(
            r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO"
            r"|true|True|TRUE|false|False|FALSE"
            r"|on|On|ON|off|Off|OFF)$", re.X),
        list("yYnNtTfFoO"))

    def _mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found duplicate key %r" % (key,), key_node.start_mark)
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _Host11Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return yaml.load(text, Loader=_Host11Loader)

#: Programmatic backend entries (the relay bridge's /v1/runs, API-server clients)
#: are not a human frontdesk. Restricting them to task APIs would break every
#: ordinary question that needs a real tool, so they keep their full toolset.
#: Only messaging surfaces get the thin-frontdesk restriction; an unknown or
#: unreadable platform stays restricted (fail-closed).
_BACKEND_PLATFORMS = frozenset({"api_server", "local", "cli"})
BACKEND_BLOCKED_TOOLS = frozenset({"durable_submit"})


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
        return {name: False for name in _FLAG_NAMES}
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("profile config must be a regular file")
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("profile config exceeds budget")

    # Match the host's hermes_yaml policy, including YAML 1.1 booleans and
    # duplicate-key rejection. Parsers are per call, never shared by threads.
    config = _load_config(raw)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("profile config must be a mapping")
    section = config.get("superagent", {})
    if not isinstance(section, dict):
        raise ValueError("superagent settings must be a mapping")
    policy = section.get("frontdesk", {})
    if not isinstance(policy, dict) or set(policy) - set(_FLAG_NAMES):
        raise ValueError("invalid frontdesk settings")
    flags = {name: policy.get(name, False) for name in _FLAG_NAMES}
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError("frontdesk flags must be boolean")
    return flags


def _allowed_tools(policy: dict[str, bool]) -> frozenset[str]:
    allowed = set(TASK_TOOLS)
    if policy.get("memory_read_enabled"):
        allowed |= MEMORY_READ_TOOLS
    if policy.get("read_tools_enabled"):
        allowed |= READ_TOOLS
    if policy.get("shell_enabled"):
        allowed |= SHELL_TOOLS
    if policy.get("hr_enabled"):
        allowed |= HR_TOOLS
    return frozenset(allowed)


def tool_block_message(tool_name: str, *, home: Path | str | None = None) -> str | None:
    try:
        policy = _settings(home)
        if not policy["enabled"]:
            return None
        if _backend_session():
            return TOOL_DENIED if isinstance(tool_name, str) and tool_name in BACKEND_BLOCKED_TOOLS else None
        allowed = _allowed_tools(policy)
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
        if _backend_session():
            return [
                item for item in definitions
                if item.get("function", {}).get("name") not in BACKEND_BLOCKED_TOOLS
            ]
        allowed = _allowed_tools(policy)
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
