"""Administrator-configured LAN node registry, health probing, and resumable remote agent RPC.

Generalizes the control-plane JSON over SSH/stdio relay pattern into a reusable,
host-governed distributed node communication layer:
1. Node definitions and RPC argv commands come exclusively from host configuration,
   never from model-supplied arguments.
2. Session affinity (``session_key -> session_id``) is persisted with POSIX file
   locking so multi-turn calls to a remote node agent resume deterministically.
3. Health probes verify real service responses and latency rather than bare TCP connect.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid

NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SESSION_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
VALID_OPERATIONS = frozenset({"submit", "status", "cancel", "reset"})


def _clean_text(value: Any, *, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}_must_be_string")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > max_len:
        raise ValueError(f"{field}_invalid_length")
    return cleaned


def normalize_nodes(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize administrator-configured LAN production nodes."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("lan_nodes_required")
    normalized: dict[str, dict[str, Any]] = {}
    for node_id, entry in raw.items():
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            raise ValueError("invalid_node_id")
        if not isinstance(entry, dict):
            raise ValueError("node_entry_must_be_mapping")
        display_name = _clean_text(
            entry.get("display_name", node_id), field="display_name", max_len=80
        )
        role = _clean_text(entry.get("role", "worker"), field="role", max_len=64)
        description = _clean_text(
            entry.get("description", role), field="description", max_len=240
        )
        raw_caps = entry.get("capabilities", [])
        if not isinstance(raw_caps, list):
            raise ValueError("capabilities_must_be_list")
        capabilities: list[str] = []
        for cap in raw_caps:
            if not isinstance(cap, str) or not CAPABILITY_RE.fullmatch(cap):
                raise ValueError("invalid_capability")
            if cap not in capabilities:
                capabilities.append(cap)

        ssh_target = entry.get("ssh_target")
        if ssh_target is not None:
            ssh_target = _clean_text(ssh_target, field="ssh_target", max_len=128)

        http_health_url = entry.get("http_health_url")
        if http_health_url is not None:
            http_health_url = _clean_text(
                http_health_url, field="http_health_url", max_len=256
            )
            if not (
                http_health_url.startswith("http://")
                or http_health_url.startswith("https://")
            ):
                raise ValueError("invalid_http_health_url")

        agent_rpc_cmd = entry.get("agent_rpc_cmd")
        if agent_rpc_cmd is not None:
            if (
                not isinstance(agent_rpc_cmd, list)
                or not agent_rpc_cmd
                or not all(isinstance(x, str) and x.strip() for x in agent_rpc_cmd)
            ):
                raise ValueError("invalid_agent_rpc_cmd")
            agent_rpc_cmd = list(agent_rpc_cmd)

        remote_model = entry.get("remote_model")
        if remote_model is not None:
            remote_model = _clean_text(remote_model, field="remote_model", max_len=64)

        normalized[node_id] = {
            "node_id": node_id,
            "display_name": display_name,
            "role": role,
            "description": description,
            "capabilities": capabilities,
            "ssh_target": ssh_target,
            "http_health_url": http_health_url,
            "agent_rpc_cmd": agent_rpc_cmd,
            "remote_model": remote_model or "default",
        }
    return normalized


def list_nodes(
    raw_nodes: Any,
    *,
    role: str | None = None,
    capability: str | None = None,
) -> dict[str, Any]:
    """Return public node directory metadata without exposing internal command argv."""
    catalog = normalize_nodes(raw_nodes)
    items = []
    for node_id, spec in catalog.items():
        if role and spec["role"] != role:
            continue
        if capability and capability not in spec["capabilities"]:
            continue
        items.append(
            {
                "node_id": node_id,
                "display_name": spec["display_name"],
                "role": spec["role"],
                "description": spec["description"],
                "capabilities": list(spec["capabilities"]),
                "has_ssh": bool(spec["ssh_target"]),
                "has_http_health": bool(spec["http_health_url"]),
                "has_agent_rpc": bool(spec["agent_rpc_cmd"]),
            }
        )
    return {"ok": True, "count": len(items), "nodes": items}


def _probe_http(url: str, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            raw = resp.read(4096).decode("utf-8", "replace")
            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            parsed = None
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
            ok = 200 <= resp.status < 300
            if isinstance(parsed, dict) and "ok" in parsed:
                ok = bool(ok and parsed["ok"])
            return {
                "ok": ok,
                "status_code": resp.status,
                "latency_ms": latency_ms,
                "summary": parsed if isinstance(parsed, dict) else raw[:200],
            }
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        return {
            "ok": False,
            "status_code": exc.code,
            "latency_ms": latency_ms,
            "error": f"http_{exc.code}",
        }
    except Exception as exc:
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": type(exc).__name__,
        }


def _probe_ssh(ssh_target: str, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    connect_timeout = max(1, min(int(timeout_s), 8))
    cmd = [
        "ssh",
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        ssh_target,
        "uname -n",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 1.0,
            stdin=subprocess.DEVNULL,
        )
        latency_ms = round((time.monotonic() - started) * 1000.0, 2)
        if proc.returncode == 0:
            return {
                "ok": True,
                "latency_ms": latency_ms,
                "remote_hostname": proc.stdout.strip()[:64],
            }
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "returncode": proc.returncode,
            "error": (proc.stderr or "").strip()[:160] or "ssh_failed",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 2),
            "error": "ssh_timeout",
        }
    except OSError as exc:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 2),
            "error": type(exc).__name__,
        }


def probe_node(
    raw_nodes: Any,
    node_id: str,
    *,
    timeout_s: float = 5.0,
    http_prober: Callable[[str, float], dict[str, Any]] = _probe_http,
    ssh_prober: Callable[[str, float], dict[str, Any]] = _probe_ssh,
) -> dict[str, Any]:
    """Probe a configured LAN production node across SSH and HTTP health surfaces."""
    catalog = normalize_nodes(raw_nodes)
    if not isinstance(node_id, str) or node_id not in catalog:
        raise ValueError("unknown_node_id")
    spec = catalog[node_id]
    timeout_s = max(1.0, min(float(timeout_s), 15.0))
    checks: dict[str, Any] = {}
    if spec["ssh_target"]:
        checks["ssh"] = ssh_prober(spec["ssh_target"], timeout_s)
    if spec["http_health_url"]:
        checks["http_health"] = http_prober(spec["http_health_url"], timeout_s)
    overall_ok = bool(checks) and all(c.get("ok") is True for c in checks.values())
    return {
        "ok": overall_ok,
        "node_id": node_id,
        "display_name": spec["display_name"],
        "role": spec["role"],
        "checks": checks,
    }


@contextmanager
def _locked_sessions(state_dir: Path, *, timeout_s: float = 10.0):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_file = state_dir / "lan_sessions.lock"
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    start_time = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() - start_time >= timeout_s:
                    raise TimeoutError(f"lan_sessions_lock_timeout_{timeout_s}s")
                time.sleep(0.05)
        sessions_file = state_dir / "lan_sessions.json"
        try:
            data = (
                json.loads(sessions_file.read_text(encoding="utf-8"))
                if sessions_file.exists()
                else {}
            )
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        yield data
        tmp = sessions_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, sessions_file)
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _parse_last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            continue
    return None


def invoke_node_rpc(
    raw_nodes: Any,
    state_dir: str | Path,
    *,
    node_id: str,
    session_key: str,
    request_id: str,
    operation: str = "submit",
    prompt: str = "",
    context: str = "",
    timeout_s: float = 60.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Execute a resumable protocol_version=2 RPC against a remote LAN node agent."""
    catalog = normalize_nodes(raw_nodes)
    if not isinstance(node_id, str) or node_id not in catalog:
        raise ValueError("unknown_node_id")
    spec = catalog[node_id]
    cmd = spec["agent_rpc_cmd"]
    if not cmd:
        raise ValueError("node_agent_rpc_not_configured")
    if not isinstance(session_key, str) or not SESSION_KEY_RE.fullmatch(session_key):
        raise ValueError("invalid_session_key")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("invalid_request_id")
    if operation not in VALID_OPERATIONS:
        raise ValueError("invalid_operation")

    state_path = Path(state_dir)
    if not state_path.is_absolute():
        raise ValueError("absolute_state_dir_required")

    composite_key = f"{node_id}:{session_key}"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if operation == "reset":
        with _locked_sessions(state_path) as sessions:
            sessions.pop(composite_key, None)
        return {
            "ok": True,
            "node_id": node_id,
            "session_key": session_key,
            "operation": "reset",
            "reset": True,
        }

    with _locked_sessions(state_path) as sessions:
        entry = sessions.get(composite_key)
        sid = entry.get("session_id") if isinstance(entry, dict) else None
        if not isinstance(sid, str) or not sid:
            sid = f"lan-{node_id}-{uuid.uuid4().hex[:16]}"
            sessions[composite_key] = {"session_id": sid, "updated_at": now_iso}

    payload: dict[str, Any] = {
        "protocol_version": 2,
        "session_key": session_key,
        "request_id": request_id,
        "operation": operation,
    }
    if operation == "submit":
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            raise ValueError("invalid_prompt")
        payload.update(
            prompt=prompt,
            context=context if isinstance(context, str) else "",
            session_id=sid,
            model=spec["remote_model"],
            allow_files=True,
        )

    started = time.monotonic()
    timeout_s = max(5.0, min(float(timeout_s), 550.0))
    try:
        proc = runner(
            cmd,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "node_id": node_id,
            "session_id": sid,
            "request_id": request_id,
            "error": "transport_timeout"
            if isinstance(exc, subprocess.TimeoutExpired)
            else "transport_error",
            "retryable": True,
            "transport_metrics": {
                "control_seconds": round(time.monotonic() - started, 4)
            },
        }

    parsed = _parse_last_json_line(proc.stdout)
    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "node_id": node_id,
            "session_id": sid,
            "request_id": request_id,
            "error": "invalid_transport_response",
            "retryable": True,
            "transport_metrics": {
                "control_seconds": round(time.monotonic() - started, 4)
            },
        }

    return {
        "ok": bool(parsed.get("ok", proc.returncode == 0)),
        "node_id": node_id,
        "session_id": sid,
        "request_id": request_id,
        "operation": operation,
        "transport_metrics": {"control_seconds": round(time.monotonic() - started, 4)},
        "remote": parsed,
    }

