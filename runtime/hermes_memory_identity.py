"""Trusted IM identity -> private memory runtime, without importing engine state.

Only the host adapter uses this module. Policy is admin-owned JSON, never tool
arguments. Every operation revalidates it, including cache reads and verdicts.
Absence of a configured router is legacy mode; a broken configured router is
never permission to use the legacy principal.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

MAX_POLICY_BYTES = 262144
MAX_BINDINGS = 256


def _mapping(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate identity policy key")
        result[key] = value
    return result


def _text(value, *, empty=False):
    if (not isinstance(value, str) or len(value) > 512
            or (not empty and not value.strip()) or any(c in value for c in "\r\n\0")):
        raise ValueError("invalid identity policy text")
    return value


def _private_directory(value):
    path = Path(_text(value))
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("private absolute memory directory required")
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise ValueError("private owned memory directory required")
    return path.resolve()


class IdentityRouter:
    def __init__(self, policy_file, *, profile=None):
        self.path = Path(_text(policy_file))
        if not self.path.is_absolute():
            raise ValueError("absolute identity policy required")
        self.profile = profile

    def _read(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
                raise ValueError("private owned identity policy required")
            raw = stream.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("identity policy exceeds budget")
        policy = json.loads(raw, object_pairs_hook=_mapping)
        if (not isinstance(policy, dict)
                or set(policy) != {"version", "profile", "profile_home", "bindings"}
                or type(policy["version"]) is not int or policy["version"] != 1):
            raise ValueError("invalid identity policy")
        profile = _text(policy["profile"])
        if self.profile is not None and profile != self.profile:
            raise ValueError("memory profile mismatch")
        home = Path(_text(policy["profile_home"]))
        if not home.is_absolute():
            raise ValueError("absolute profile home required")
        rows = policy["bindings"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BINDINGS:
            raise ValueError("invalid identity bindings")
        routes, selectors, principals, roots = [], set(), {}, {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {
                    "platform", "scope_id", "user_id", "chat_id", "principal", "agent_id", "state_dir"}):
                raise ValueError("invalid identity binding")
            selector = tuple(_text(row[k], empty=k == "scope_id")
                             for k in ("platform", "scope_id", "user_id", "chat_id"))
            if selector in selectors:
                raise ValueError("ambiguous identity binding")
            selectors.add(selector)
            principal = (_text(row["principal"]), _text(row["agent_id"]))
            root = _private_directory(row["state_dir"])
            data, spool = root / "data", root / "spool"
            if _private_directory(str(data)) != data or _private_directory(str(spool)) != spool:
                raise ValueError("memory directory alias refused")
            if (data / "coding_memory_cv.db").is_symlink():
                raise ValueError("memory database alias refused")
            if principal in principals and principals[principal] != root:
                raise ValueError("principal has conflicting runtime")
            for other, owner in roots.items():
                if (root == other or root in other.parents or other in root.parents) and owner != principal:
                    raise ValueError("memory runtime shared across principals")
            principals[principal], roots[root] = root, principal
            routes.append((selector, {"profile": profile, "user_id": principal[0],
                                     "agent_id": principal[1], "data_dir": str(data),
                                     "spool_dir": str(spool)}))
        return home.resolve(), routes, hashlib.sha256(raw).hexdigest()

    def resolve(self, session_id):
        """Return an authorized immutable-by-convention route, or raise/deny.

        Direct ContextVar values are required. get_session_env's OS environment
        fallback is intentionally not an authentication source.
        """
        from gateway import session_context
        from agent.secret_scope import is_multiplex_active
        from agent.delegation_context import is_delegated_child_process_context
        from hermes_constants import get_hermes_home, get_hermes_home_override

        if is_delegated_child_process_context():
            return None
        if is_multiplex_active() and not get_hermes_home_override():
            return None
        home, routes, revision = self._read()
        if get_hermes_home().resolve() != home:
            return None

        def bound(name):
            var = session_context._VAR_MAP[name]
            value = var.get()
            if not isinstance(value, str):
                raise ValueError("authenticated memory context missing")
            return _text(value, empty=True)

        if (not isinstance(session_id, str) or not session_id.strip()
                or bound("HERMES_SESSION_ID") != session_id
                or not bound("HERMES_SESSION_KEY") or not bound("HERMES_SESSION_CHAT_ID")
                or bound("HERMES_SESSION_CHAT_TYPE") != "dm"
                or bound("HERMES_CRON_SESSION") != ""):
            return None
        selector = tuple(bound("HERMES_SESSION_" + key)
                         for key in ("PLATFORM", "SCOPE_ID", "USER_ID", "CHAT_ID"))
        for expected, route in routes:
            if selector == expected:
                # Includes the policy revision so revocation/remapping cannot
                # reuse an old background result or a captured turn verdict.
                scope = hashlib.sha256(json.dumps([revision, selector, route],
                                                  sort_keys=True).encode()).hexdigest()
                return dict(route, cache_scope=scope)
        return None

    def runtimes(self, *, home):
        """Validated admin-only inventory; never reads memory or host sessions."""
        expected_home, routes, _ = self._read()
        if Path(home).resolve() != expected_home:
            raise ValueError("memory profile home mismatch")
        unique = {(r['user_id'], r['agent_id']): r for _, r in routes}
        return [dict(route) for route in unique.values()]

    def runtime(self, *, home, user_id, agent_id):
        """Admin-only worker planning; this is not a model-callable authority."""
        for route in self.runtimes(home=home):
            if (route['user_id'], route['agent_id']) == (user_id, agent_id):
                return dict(route)
        raise ValueError("memory runtime not registered")
