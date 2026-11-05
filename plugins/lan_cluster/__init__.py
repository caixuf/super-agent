"""Opt-in Hermes plugin for LAN production node discovery, health probing, and resumable remote agent RPC."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _engine(ctx):
    try:
        from agentcore import lan_cluster
        return lan_cluster
    except ModuleNotFoundError as exc:
        if exc.name not in ("agentcore", "agentcore.lan_cluster"):
            raise
    root = ctx.get_config("engine_root", "")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ValueError("absolute administrator engine_root required")
    source = Path(root).resolve(strict=True) / "agentcore/lan_cluster.py"
    spec = importlib.util.spec_from_file_location("lan_cluster_engine", source)
    if spec is None or spec.loader is None:
        raise ImportError("lan_cluster engine unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register(ctx):
    if ctx.get_config("enabled", False) is not True:
        return
    engine = _engine(ctx)
    engine.normalize_nodes(ctx.get_config("nodes", None))

    def handle_list(args, *, session_id=None, **_kwargs):
        if ctx.get_config("enabled", False) is not True:
            return json.dumps({"ok": False, "error": "lan_cluster_disabled"})
        if not isinstance(session_id, str) or not session_id.strip():
            return json.dumps({"ok": False, "error": "session_required"})
        if not isinstance(args, dict):
            return json.dumps({"ok": False, "error": "invalid_arguments"})
        try:
            res = engine.list_nodes(
                ctx.get_config("nodes", None),
                role=args.get("role"),
                capability=args.get("capability"),
            )
            return json.dumps(res, ensure_ascii=False)
        except Exception:
            return json.dumps({"ok": False, "error": "lan_node_list_failed"})

    def handle_probe(args, *, session_id=None, **_kwargs):
        if ctx.get_config("enabled", False) is not True:
            return json.dumps({"ok": False, "error": "lan_cluster_disabled"})
        if not isinstance(session_id, str) or not session_id.strip():
            return json.dumps({"ok": False, "error": "session_required"})
        if not isinstance(args, dict) or "node_id" not in args:
            return json.dumps({"ok": False, "error": "invalid_arguments"})
        try:
            res = engine.probe_node(
                ctx.get_config("nodes", None),
                args["node_id"],
                timeout_s=args.get("timeout_s", 5.0),
            )
            return json.dumps(res, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception:
            return json.dumps({"ok": False, "error": "lan_node_probe_failed"})

    def handle_rpc(args, *, session_id=None, **_kwargs):
        if ctx.get_config("enabled", False) is not True:
            return json.dumps({"ok": False, "error": "lan_cluster_disabled"})
        if not isinstance(session_id, str) or not session_id.strip():
            return json.dumps({"ok": False, "error": "session_required"})
        if not isinstance(args, dict) or not {"node_id", "request_id"}.issubset(args):
            return json.dumps({"ok": False, "error": "invalid_arguments"})
        state_dir = ctx.get_config("state_dir", "")
        try:
            res = engine.invoke_node_rpc(
                ctx.get_config("nodes", None),
                state_dir,
                node_id=args["node_id"],
                session_key=args.get("session_key") or session_id,
                request_id=args["request_id"],
                operation=args.get("operation", "submit"),
                prompt=args.get("prompt", ""),
                context=args.get("context", ""),
                timeout_s=args.get("timeout_s", 60.0),
            )
            return json.dumps(res, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception:
            return json.dumps({"ok": False, "error": "lan_node_rpc_failed"})

    ctx.register_tool(
        name="lan_node_list",
        toolset="lan_cluster",
        description="List administrator-configured LAN production nodes, roles, and capabilities.",
        schema={
            "name": "lan_node_list",
            "description": "List administrator-configured LAN production nodes, roles, and capabilities.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "role": {"type": "string", "maxLength": 64},
                    "capability": {"type": "string", "maxLength": 32},
                },
                "required": [],
            },
        },
        handler=handle_list,
    )

    ctx.register_tool(
        name="lan_node_probe",
        toolset="lan_cluster",
        description="Probe reachability, SSH identity, and HTTP service health of a registered LAN production node.",
        schema={
            "name": "lan_node_probe",
            "description": "Probe reachability, SSH identity, and HTTP service health of a registered LAN production node.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]{0,31}$",
                        "description": "Registered LAN node identifier from lan_node_list.",
                    },
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 15},
                },
                "required": ["node_id"],
            },
        },
        handler=handle_probe,
    )

    ctx.register_tool(
        name="lan_node_rpc",
        toolset="lan_cluster",
        description="Invoke, poll, cancel, or reset a remote agent on a registered LAN production node via resumable protocol_version=2 RPC.",
        schema={
            "name": "lan_node_rpc",
            "description": "Invoke, poll, cancel, or reset a remote agent on a registered LAN production node via resumable protocol_version=2 RPC.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]{0,31}$",
                    },
                    "request_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["submit", "status", "cancel", "reset"],
                    },
                    "prompt": {
                        "type": "string",
                        "maxLength": 100000,
                    },
                    "context": {
                        "type": "string",
                        "maxLength": 20000,
                    },
                    "session_key": {
                        "type": "string",
                        "maxLength": 200,
                    },
                    "timeout_s": {"type": "number", "minimum": 5, "maximum": 550},
                },
                "required": ["node_id", "request_id"],
            },
        },
        handler=handle_rpc,
    )

    ctx.register_system_prompt_section(
        id="lan-cluster-routing",
        content=(
            "When coordinating across LAN production machines, use lan_node_list and lan_node_probe "
            "to inspect registered nodes and their live health before dispatching work. "
            "Use lan_node_rpc for resumable remote agent calls on nodes with agent_rpc capability."
        ),
        position="after_memory",
        max_chars=400,
    )

