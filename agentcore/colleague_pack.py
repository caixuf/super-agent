"""Portable colleague pack discipline (Prefer B thin).

OpenMausBot pattern citation only — map onto colleague_catalog / colleague_dev.
No vendor trees. Pack text is untrusted selection metadata (same NOTICE as
colleague_catalog); it grants no dispatch permissions, never auto-hires, and
never silently overwrites existing role definitions.

Public surface:
  parse_pack · redact_pack · validate_pack · import_additive · export_redacted
"""
from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Union

from agentcore.colleague_catalog import (
    FIELDS,
    MAX_ROLES,
    NOTICE,
    ROLE_PATTERN,
    normalize_catalog,
)

PACK_FORMAT = "colleague_pack/v1"
PACK_NOTICE = NOTICE

# Keys that must never appear in accepted pack bytes (case-insensitive match on
# leaf key names). Pattern citation: OpenMausBot "no secrets in packs".
_BANNED_KEY_RE = re.compile(
    r"(?i)\A(.*_)?(secret|token|api[_-]?key|password|passwd|cookie|"
    r"credential|authorization|auth[_-]?header|private[_-]?key|"
    r"access[_-]?key|session[_-]?id|computer[_-]?handle|"
    r"messaging[_-]?credential|electron|jev|harness)s?\Z"
)

# High-entropy / secret-shaped string values inside otherwise-allowed fields.
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S{8,})"
)

# Absolute host paths are banned inside pack bytes (portable packs only).
_ABS_HOST_PATH_RE = re.compile(r"(?i)\A(?:/[A-Za-z]|[A-Za-z]:[\\/]|\\\\)")

Source = Union[str, bytes, Path, Mapping[str, Any]]

# Host/tests may inject yaml.safe_load; agentcore never imports PyYAML.
_YAML_SAFE_LOAD: Optional[Any] = None


class PackError(ValueError):
    """Honest pack parse / validate / import failure (never silent)."""


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise PackError("pack_source_must_be_text_bytes_path_or_mapping")


def _load_mapping(source: Source) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, Path) or (
        isinstance(source, str) and ("\n" not in source) and Path(source).is_file()
    ):
        path = Path(source)
        text = path.read_text(encoding="utf-8")
        return _parse_text(text, hint=path.suffix.lower())
    text = _as_text(source)
    return _parse_text(text, hint="")


def _parse_text(text: str, *, hint: str) -> dict[str, Any]:
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        raise PackError("empty_pack")
    # Prefer JSON for machine import.
    if hint == ".json" or stripped[0] in "{[":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PackError(f"invalid_pack_json:{exc.msg}") from exc
        if not isinstance(data, dict):
            raise PackError("pack_root_must_be_object")
        return data
    # YAML-frontmatter + MD playbook (optional; soft-dep on PyYAML).
    if stripped.startswith("---"):
        return _parse_frontmatter_md(stripped)
    raise PackError("unsupported_pack_serialization")


def set_yaml_loader(loader: Optional[Any]) -> None:
    """Inject or clear a callable ``loader(text) -> mapping`` for MD frontmatter.

    Prefer-B JSON packs need no loader. MD frontmatter packs work only when the
    host/tests inject a YAML safe loader from outside agentcore.
    agentcore itself never imports PyYAML — the import-boundary gate forbids it.
    """
    global _YAML_SAFE_LOAD
    if loader is not None and not callable(loader):
        raise PackError("yaml_loader_must_be_callable")
    _YAML_SAFE_LOAD = loader


def _parse_frontmatter_md(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        raise PackError("frontmatter_expected")
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        raise PackError("frontmatter_unclosed")
    fm_text = rest[:end].strip("\n")
    body = rest[end + 4:].lstrip("\n")
    loader = _YAML_SAFE_LOAD
    if loader is None:
        raise PackError("yaml_unavailable_use_json_pack")
    try:
        meta = loader(fm_text)
    except Exception as exc:  # noqa: BLE001 — surface as PackError
        raise PackError(f"invalid_pack_yaml:{exc}") from exc
    if not isinstance(meta, dict):
        raise PackError("frontmatter_must_be_object")
    if body and "playbook" not in meta:
        meta["playbook"] = body
    return meta


def _walk_banned(obj: Any, *, path: str = "$") -> None:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise PackError(f"non_string_key:{path}")
            if _BANNED_KEY_RE.fullmatch(key):
                raise PackError(f"banned_key:{path}.{key}")
            _walk_banned(value, path=f"{path}.{key}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_banned(item, path=f"{path}[{i}]")
    elif isinstance(obj, str):
        if _SECRET_VALUE_RE.search(obj):
            raise PackError(f"secret_shaped_value:{path}")
        if _ABS_HOST_PATH_RE.match(obj.strip()):
            raise PackError(f"absolute_host_path_banned:{path}")
        if any(unicodedata.category(c).startswith("C") and c not in "\n\t" for c in obj):
            raise PackError(f"control_char_banned:{path}")


def _role_fields(entry: Mapping[str, Any]) -> dict[str, Any]:
    missing = FIELDS - set(entry)
    extra_field_keys = set(entry) - FIELDS - {
        "role_id", "role", "skills", "routines", "playbook",
    }
    if missing:
        raise PackError(f"missing_fields:{sorted(missing)}")
    if extra_field_keys:
        raise PackError(f"unknown_role_keys:{sorted(extra_field_keys)}")
    return {
        "display_name": entry["display_name"],
        "duty": entry["duty"],
        "can_do": entry["can_do"],
        "boundaries": entry["boundaries"],
    }


def _normalize_skill_refs(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 32:
        raise PackError("invalid_skills")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            skill_id = item.strip()
        elif isinstance(item, Mapping):
            skill_id = str(item.get("id") or item.get("path") or "").strip()
        else:
            raise PackError("invalid_skill_ref")
        if not skill_id or len(skill_id) > 128:
            raise PackError("invalid_skill_id")
        if _ABS_HOST_PATH_RE.match(skill_id):
            raise PackError("skill_absolute_path_banned")
        if skill_id in seen:
            raise PackError("duplicate_skill_ref")
        seen.add(skill_id)
        # Import discipline: always land disabled / default off.
        out.append({"id": skill_id, "enabled": False})
    return out


def _normalize_routine_refs(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 32:
        raise PackError("invalid_routines")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            routine_id = item.strip()
        elif isinstance(item, Mapping):
            routine_id = str(item.get("id") or "").strip()
        else:
            raise PackError("invalid_routine_ref")
        if not routine_id or len(routine_id) > 128:
            raise PackError("invalid_routine_id")
        if routine_id in seen:
            raise PackError("duplicate_routine_ref")
        seen.add(routine_id)
        # Import discipline: paused / not scheduled. No scheduler invented —
        # metadata only until a routine seam exists.
        out.append({"id": routine_id, "paused": True})
    return out


def _iter_role_entries(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    roles = data.get("roles")
    if roles is None:
        raise PackError("roles_required")
    entries: list[dict[str, Any]] = []
    if isinstance(roles, Mapping):
        for role_id, meta in roles.items():
            if not isinstance(meta, Mapping):
                raise PackError("role_entry_must_be_object")
            item = dict(meta)
            item.setdefault("role_id", role_id)
            entries.append(item)
    elif isinstance(roles, list):
        for item in roles:
            if not isinstance(item, Mapping):
                raise PackError("role_entry_must_be_object")
            entries.append(dict(item))
    else:
        raise PackError("roles_must_be_list_or_object")
    if not entries:
        raise PackError("roles_empty")
    return entries


def _canonical_role(entry: Mapping[str, Any]) -> dict[str, Any]:
    role_id = entry.get("role_id") or entry.get("role")
    if not isinstance(role_id, str) or not ROLE_PATTERN.fullmatch(role_id):
        raise PackError("invalid_role_id")
    fields = _role_fields(entry)
    # Validate through catalog ontology (FIELDS / size / control chars).
    normalize_catalog({role_id: fields})
    playbook = entry.get("playbook")
    if playbook is None:
        playbook_text = None
    elif isinstance(playbook, str):
        playbook_text = playbook.strip()
        if len(playbook_text) > 8000:
            raise PackError("playbook_too_large")
        if playbook_text == "":
            playbook_text = None
    else:
        raise PackError("playbook_must_be_string")
    return {
        "role_id": role_id,
        **fields,
        "skills": _normalize_skill_refs(entry.get("skills")),
        "routines": _normalize_routine_refs(entry.get("routines")),
        "playbook": playbook_text,
    }


def redact_pack(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached copy with banned keys dropped and secret-shaped
    strings replaced by ``[REDACTED]``. Does not invent new roles.
    """
    def scrub(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for key, value in obj.items():
                if isinstance(key, str) and _BANNED_KEY_RE.fullmatch(key):
                    continue
                out[key] = scrub(value)
            return out
        if isinstance(obj, list):
            return [scrub(x) for x in obj]
        if isinstance(obj, str):
            if _SECRET_VALUE_RE.search(obj) or _ABS_HOST_PATH_RE.match(obj.strip()):
                return "[REDACTED]"
            return obj
        return obj

    redacted = scrub(dict(data))
    if not isinstance(redacted, dict):
        raise PackError("redact_failed")
    redacted.setdefault("pack_format", PACK_FORMAT)
    redacted.setdefault("notice", PACK_NOTICE)
    return redacted


def parse_pack(source: Source) -> dict[str, Any]:
    """Load pack bytes/mapping, reject banned secret shape, return raw mapping.

    Does not yet run catalog field validation — use ``validate_pack`` next.
    """
    data = _load_mapping(source)
    _walk_banned(data)
    fmt = data.get("pack_format", PACK_FORMAT)
    if fmt not in (PACK_FORMAT, "colleague_pack/v1", None):
        # Accept missing format for thin JSON; reject foreign formats.
        if fmt is not None and str(fmt) != PACK_FORMAT:
            raise PackError(f"unsupported_pack_format:{fmt}")
    return data


def validate_pack(source: Source) -> dict[str, Any]:
    """Parse + catalog-validate; return normalized pack snapshot."""
    if isinstance(source, Mapping) and source.get("_validated") is True:
        data = dict(source)
    else:
        data = parse_pack(source)
    entries = [_canonical_role(e) for e in _iter_role_entries(data)]
    role_ids = [e["role_id"] for e in entries]
    if len(role_ids) != len(set(role_ids)):
        raise PackError("duplicate_role_id")
    if len(entries) > MAX_ROLES:
        raise PackError("pack_exceeds_max_roles")
    # Shared playbook at pack root is optional descriptive sidecar.
    root_playbook = data.get("playbook")
    if root_playbook is not None and not isinstance(root_playbook, str):
        raise PackError("playbook_must_be_string")
    if isinstance(root_playbook, str) and len(root_playbook) > 8000:
        raise PackError("playbook_too_large")
    pack = {
        "pack_format": PACK_FORMAT,
        "notice": PACK_NOTICE,
        "roles": entries,
        "playbook": root_playbook.strip() if isinstance(root_playbook, str) and root_playbook.strip() else None,
        "_validated": True,
    }
    # Re-check size via catalog normalize of role fields alone.
    catalog_roles = {
        e["role_id"]: {k: e[k] for k in FIELDS} for e in entries
    }
    normalize_catalog(catalog_roles)
    return pack


def _fields_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    try:
        left = normalize_catalog({"x": {k: a[k] for k in FIELDS}})["colleagues"][0]
        right = normalize_catalog({"x": {k: b[k] for k in FIELDS}})["colleagues"][0]
    except (ValueError, KeyError, PackError):
        return False
    return (
        left["display_name"] == right["display_name"]
        and left["duty"] == right["duty"]
        and left["can_do"] == right["can_do"]
        and left["boundaries"] == right["boundaries"]
    )


def import_additive(
    base_roles: Optional[Mapping[str, Any]],
    pack: Source,
) -> dict[str, Any]:
    """Merge pack roles into an administrator catalog snapshot (additive only).

    - Never wipes ``DEV_ROLES`` / ``default_dev_catalog``; caller supplies the
      snapshot to extend.
    - Same ``role_id`` + identical FIELDS → idempotent no-op.
    - Same ``role_id`` + different FIELDS → honest ``PackError`` (no overwrite).
    - New ``role_id`` → append under ``MAX_ROLES``.
    - skills land ``enabled=false``; routines land ``paused=true`` (metadata).
    - Ontology only — does **not** auto-hire / touch colleague_registry.
    """
    validated = validate_pack(pack)
    base: dict[str, Any] = {}
    if base_roles:
        if not isinstance(base_roles, Mapping):
            raise PackError("base_roles_must_be_mapping")
        # Detach; validate existing snapshot through catalog when non-empty.
        base = deepcopy(dict(base_roles))
        if base:
            normalize_catalog(base)

    added: list[str] = []
    unchanged: list[str] = []
    skill_refs: list[dict[str, Any]] = []
    routine_refs: list[dict[str, Any]] = []

    for entry in validated["roles"]:
        role_id = entry["role_id"]
        fields = {k: entry[k] for k in FIELDS}
        if role_id in base:
            if _fields_equal(base[role_id], fields):
                unchanged.append(role_id)
            else:
                raise PackError(f"role_conflict:{role_id}")
        else:
            if len(base) + 1 > MAX_ROLES:
                raise PackError("max_roles_exceeded")
            base[role_id] = deepcopy(fields)
            added.append(role_id)
        for sk in entry["skills"]:
            skill_refs.append({"role_id": role_id, **sk})
        for rt in entry["routines"]:
            routine_refs.append({"role_id": role_id, **rt})

    # Final catalog check (empty base + empty pack already rejected by validate).
    catalog = normalize_catalog(base)
    return {
        "ok": True,
        "roles": base,
        "catalog": catalog,
        "added": added,
        "unchanged": unchanged,
        "skills": skill_refs,  # all enabled=False
        "routines": routine_refs,  # all paused=True; metadata-only (no scheduler)
        "notice": PACK_NOTICE,
        "routines_seam": "metadata_only",
        "deliver_or_reason": (
            "routines stored as paused metadata references; "
            "no scheduler seam in this knife — do not invent one"
        ),
    }


def export_redacted(
    roles: Mapping[str, Any],
    *,
    skills: Optional[Mapping[str, Any]] = None,
    routines: Optional[Mapping[str, Any]] = None,
    playbook: Optional[str] = None,
    role_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Export roles as a portable pack: secrets stripped, skills off, routines paused.

    ``skills`` / ``routines`` maps are optional ``{role_id: [id, ...]}`` reference
    lists. Absolute paths and secret-shaped strings are redacted/omitted.
    """
    if not isinstance(roles, Mapping) or not roles:
        raise PackError("roles_required")
    selected = dict(roles)
    if role_ids is not None:
        missing = [r for r in role_ids if r not in selected]
        if missing:
            raise PackError(f"unknown_export_roles:{missing}")
        selected = {r: selected[r] for r in role_ids}
    # Validate ontology first.
    normalize_catalog(selected)

    skill_map = skills or {}
    routine_map = routines or {}
    entries: list[dict[str, Any]] = []
    for role_id in sorted(selected):
        meta = selected[role_id]
        fields = {k: meta[k] for k in FIELDS}
        sk_raw = skill_map.get(role_id, [])
        rt_raw = routine_map.get(role_id, [])
        entry = {
            "role_id": role_id,
            **fields,
            "skills": _normalize_skill_refs(sk_raw),
            "routines": _normalize_routine_refs(rt_raw),
        }
        entries.append(entry)

    pack: dict[str, Any] = {
        "pack_format": PACK_FORMAT,
        "notice": PACK_NOTICE,
        "roles": entries,
    }
    if playbook:
        if not isinstance(playbook, str):
            raise PackError("playbook_must_be_string")
        pack["playbook"] = playbook
    return redact_pack(pack)


def dump_pack_json(pack: Mapping[str, Any], *, indent: int = 2) -> str:
    """Serialize a pack mapping to JSON text (machine-import preferred form)."""
    payload = dict(pack)
    payload.pop("_validated", None)
    payload.setdefault("pack_format", PACK_FORMAT)
    payload.setdefault("notice", PACK_NOTICE)
    return json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"


def load_pack_file(path: Union[str, Path]) -> dict[str, Any]:
    """Validate a pack file at an absolute host path (host-gate helper)."""
    p = Path(path)
    if not p.is_absolute():
        raise PackError("pack_path_must_be_absolute")
    if not p.is_file():
        raise PackError("pack_file_not_found")
    return validate_pack(p)


__all__ = [
    "PACK_FORMAT",
    "PACK_NOTICE",
    "PackError",
    "set_yaml_loader",
    "parse_pack",
    "redact_pack",
    "validate_pack",
    "import_additive",
    "export_redacted",
    "dump_pack_json",
    "load_pack_file",
]
