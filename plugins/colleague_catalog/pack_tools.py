"""Optional host-gated colleague pack import/export tools (Prefer B).

Default off. Requires plugin ``settings.enabled is True`` and
``settings.pack_import_export is True``. Pack paths must be absolute host
paths — model-writable import without a host gate is banned.
"""
from __future__ import annotations

import json
from pathlib import Path


def _pack_engine(ctx):
    try:
        from agentcore.colleague_pack import (
            PackError,
            dump_pack_json,
            export_redacted,
            import_additive,
            load_pack_file,
        )
        return PackError, dump_pack_json, export_redacted, import_additive, load_pack_file
    except ModuleNotFoundError as exc:
        if exc.name not in ("agentcore", "agentcore.colleague_pack"):
            raise
    import importlib.util
    root = ctx.get_config("engine_root", "")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("absolute administrator engine_root required")
    source = Path(root).resolve(strict=True) / "agentcore/colleague_pack.py"
    spec = importlib.util.spec_from_file_location("colleague_pack_engine", source)
    if spec is None or spec.loader is None:
        raise ImportError("colleague pack engine unavailable")
    module = importlib.util.module_from_spec(spec)
    # Ensure colleague_catalog importable for pack module.
    import sys
    engine_root = str(Path(root).resolve(strict=True))
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    spec.loader.exec_module(module)
    return (
        module.PackError,
        module.dump_pack_json,
        module.export_redacted,
        module.import_additive,
        module.load_pack_file,
    )


def _pack_enabled(ctx) -> bool:
    return (
        ctx.get_config("enabled", False) is True
        and ctx.get_config("pack_import_export", False) is True
    )


def register_pack_tools(ctx) -> None:
    """Register import/export tools when the administrator opts in."""
    if not _pack_enabled(ctx):
        return
    PackError, dump_pack_json, export_redacted, import_additive, load_pack_file = _pack_engine(ctx)

    def _session_ok(session_id) -> bool:
        return isinstance(session_id, str) and bool(session_id.strip()) and len(session_id) <= 512

    def import_handler(args, *, session_id=None, task_id=None, user_task=None):
        try:
            if not _pack_enabled(ctx):
                return json.dumps({"ok": False, "error": "pack_import_export_disabled"})
            if not _session_ok(session_id):
                return json.dumps({"ok": False, "error": "catalog_session_required"})
            if not isinstance(args, dict) or set(args) - {"path"}:
                return json.dumps({"ok": False, "error": "invalid_pack_args"})
            path = args.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                return json.dumps({"ok": False, "error": "absolute_pack_path_required"})
            base_roles = ctx.get_config("roles", None)
            result = import_additive(base_roles, load_pack_file(path))
            # Do not mutate plugin settings from the model path — return the
            # merged snapshot for the administrator host to persist.
            return json.dumps({
                "ok": True,
                "added": result["added"],
                "unchanged": result["unchanged"],
                "skills": result["skills"],
                "routines": result["routines"],
                "routines_seam": result["routines_seam"],
                "notice": result["notice"],
                "roles": result["roles"],
            }, ensure_ascii=False)
        except PackError:
            return json.dumps({"ok": False, "error": "invalid_or_conflicting_pack"})
        except Exception:
            return json.dumps({"ok": False, "error": "pack_import_unavailable"})

    def export_handler(args, *, session_id=None, task_id=None, user_task=None):
        try:
            if not _pack_enabled(ctx):
                return json.dumps({"ok": False, "error": "pack_import_export_disabled"})
            if not _session_ok(session_id):
                return json.dumps({"ok": False, "error": "catalog_session_required"})
            if not isinstance(args, dict) or set(args) - {"path", "role"}:
                return json.dumps({"ok": False, "error": "invalid_pack_args"})
            path = args.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                return json.dumps({"ok": False, "error": "absolute_pack_path_required"})
            roles = ctx.get_config("roles", None)
            if not isinstance(roles, dict):
                return json.dumps({"ok": False, "error": "catalog_roles_required"})
            role_filter = args.get("role")
            role_ids = None
            if role_filter is not None:
                if not isinstance(role_filter, str):
                    return json.dumps({"ok": False, "error": "invalid_pack_args"})
                role_ids = [role_filter]
            pack = export_redacted(roles, role_ids=role_ids)
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(dump_pack_json(pack), encoding="utf-8")
            return json.dumps({
                "ok": True,
                "path": str(out),
                "role_count": len(pack["roles"]),
                "notice": pack["notice"],
            }, ensure_ascii=False)
        except PackError:
            return json.dumps({"ok": False, "error": "invalid_or_conflicting_pack"})
        except Exception:
            return json.dumps({"ok": False, "error": "pack_export_unavailable"})

    import_name = "colleague_pack_import"
    export_name = "colleague_pack_export"
    import_desc = (
        "Import a portable colleague pack from an absolute host path (additive only). "
        "Skills land disabled; routines land paused metadata. Pack text grants no permissions."
    )
    export_desc = (
        "Export administrator catalog roles to an absolute host path as a redacted "
        "colleague pack (skills off, routines paused). Pack text grants no permissions."
    )
    path_param = {"type": "string", "minLength": 1, "maxLength": 1024}
    ctx.register_tool(
        name=import_name,
        toolset="colleague_catalog",
        description=import_desc,
        schema={
            "name": import_name,
            "description": import_desc,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": path_param},
                "required": ["path"],
            },
        },
        handler=import_handler,
    )
    ctx.register_tool(
        name=export_name,
        toolset="colleague_catalog",
        description=export_desc,
        schema={
            "name": export_name,
            "description": export_desc,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": path_param,
                    "role": {
                        "type": "string",
                        "maxLength": 32,
                        "pattern": "^[a-z][a-z0-9_]{0,31}$",
                    },
                },
                "required": ["path"],
            },
        },
        handler=export_handler,
    )
