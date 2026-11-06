"""Declarative business profile specification and runtime scaffolding.

Super Agent remains strictly neutral: the core framework provides queue
coordination, attempt supervision, model routing, and proactive probe
dispatching. Specific business domains (such as continuous integration runners,
hardware-in-the-loop test benches, or automated triage experts) define their
operational parameters via a Business Profile Specification.

This module provides:
1. Strong validation for domain profile specifications (workers, hooks, memory, quotas).
2. Pure deterministic rendering of systemd service and timer units.
3. Queue directory layout generation and validation.
4. Packaging and un-packaging helpers for portable distribution bundles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from typing import Any, Dict, List, Optional, Tuple

_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z")
_VALID_MODEL_TIERS = frozenset({"pro", "flash", "main"})


def quote_systemd(value: str) -> str:
    """Format string safely for systemd Environment assignment."""
    if any(c in value for c in "\n\r\0"):
        raise ValueError("newline or NUL in systemd property value")
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class WorkerSpec:
    """Specification of an execution worker role within a business profile."""
    role: str
    model_tier: Optional[str] = None
    workdir: str = ""
    duty: Optional[str] = None
    target_host: Optional[str] = None
    concurrency: int = 1
    timeout: int = 600
    max_file: int = 67108864
    max_turns: int = 1000
    run_budget: int = 3600
    env: Dict[str, str] = field(default_factory=dict)

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not _ROLE_RE.fullmatch(self.role):
            errors.append(f"Invalid worker role '{self.role}': must match {_ROLE_RE.pattern}")
        if self.model_tier and self.model_tier not in _VALID_MODEL_TIERS:
            errors.append(
                f"Invalid model_tier '{self.model_tier}' for role '{self.role}': "
                f"must be one of {sorted(_VALID_MODEL_TIERS)}"
            )
        if not (1 <= self.concurrency <= 32):
            errors.append(f"Invalid concurrency {self.concurrency}: must be between 1 and 32")
        if self.timeout <= 0:
            errors.append(f"Timeout must be positive, got {self.timeout}")
        if self.max_file <= 0:
            errors.append(f"max_file must be positive, got {self.max_file}")
        return errors


@dataclass
class ProactiveHooksSpec:
    """Specification for scheduled proactive probe watchdogs."""
    config_file: str
    timer_schedule: str = "*:00/30"
    db: Optional[str] = None
    liveness_db: Optional[str] = None

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.config_file or any(c in self.config_file for c in "\r\n\0"):
            errors.append("config_file path must be a non-empty string without newlines")
        if not self.timer_schedule or any(c in self.timer_schedule for c in "\r\n\0"):
            errors.append("timer_schedule must be a non-empty systemd calendar specification")
        return errors


@dataclass
class MemorySpec:
    """Specification for domain memory synchronization."""
    source: str
    user: str = "operator"
    agent: str = "expert"

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.source or any(c in self.source for c in "\r\n\0"):
            errors.append("memory source must be a valid path string")
        return errors


@dataclass
class GatewaySpec:
    """Specification for gateway integration."""
    port: int = 8080
    service_name: Optional[str] = None

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not (1024 <= self.port <= 65535):
            errors.append(f"Gateway port {self.port} must be between 1024 and 65535")
        return errors


@dataclass
class BusinessProfileSpec:
    """Complete declarative configuration of a business domain expert deployment."""
    profile_id: str
    description: str = ""
    version: str = "1.0.0"
    managed_root: Optional[str] = None
    profile_home: Optional[str] = None
    engine_root: Optional[str] = None
    python_bin: str = sys.executable
    gateway: Optional[GatewaySpec] = None
    workers: List[WorkerSpec] = field(default_factory=list)
    proactive_hooks: Optional[ProactiveHooksSpec] = None
    memory: Optional[MemorySpec] = None
    env: Dict[str, str] = field(default_factory=dict)
    base_dir: Optional[Path] = None

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not _IDENT_RE.fullmatch(self.profile_id):
            errors.append(f"Invalid profile_id '{self.profile_id}': must match {_IDENT_RE.pattern}")
        seen_roles: set[str] = set()
        for w in self.workers:
            if w.role in seen_roles:
                errors.append(f"Duplicate worker role defined: '{w.role}'")
            seen_roles.add(w.role)
            errors.extend(w.validate())
        if self.proactive_hooks:
            errors.extend(self.proactive_hooks.validate())
        if self.memory:
            errors.extend(self.memory.validate())
        if self.gateway:
            errors.extend(self.gateway.validate())
        return errors

    def resolve_path(self, path_str: Optional[str], default: Optional[Path] = None) -> Path:
        """Resolve a path relative to base_dir if relative, else return absolute."""
        if not path_str:
            if default is not None:
                return default
            return Path.cwd()
        p = Path(path_str)
        if p.is_absolute():
            return p
        if self.base_dir:
            return (self.base_dir / p).resolve()
        return p.resolve()

    def get_profile_home(self) -> Path:
        if self.profile_home:
            return Path(self.profile_home)
        if self.managed_root:
            return Path(self.managed_root) / "profiles" / self.profile_id
        return Path.home() / ".hermes" / "profiles" / self.profile_id

    def get_engine_root(self) -> Path:
        if self.engine_root:
            return Path(self.engine_root)
        if self.managed_root:
            return Path(self.managed_root) / "current"
        return Path(__file__).resolve().parents[1]

    def get_queue_root(self, role: str) -> Path:
        return Path.home() / ".local" / "state" / "agentcore" / "queue" / self.profile_id / role

    def worker_unit_name(self, role: str) -> str:
        return f"superagent-worker-{self.profile_id}-{role}.service"

    def hooks_unit_name(self) -> str:
        return f"superagent-proactive-hooks-{self.profile_id}.service"

    def hooks_timer_name(self) -> str:
        return f"superagent-proactive-hooks-{self.profile_id}.timer"

    def memory_unit_name(self) -> str:
        return f"superagent-memory-{self.profile_id}.service"

    def render_worker_service(self, worker: WorkerSpec) -> str:
        profile_home = str(self.get_profile_home())
        engine_root = str(self.get_engine_root())
        queue_root = str(self.get_queue_root(worker.role))
        runner_config = str(self.get_profile_home() / "worker" / "dev-runner.json")

        tier = worker.model_tier or "flash"
        env_lines: List[str] = [
            f'Environment={quote_systemd(f"DEV_MODEL_ROLE={worker.role}")}',
            f'Environment={quote_systemd(f"DEV_MODEL_TIER={tier}")}',
            f'Environment={quote_systemd(f"DEV_BUDGET={worker.run_budget}")}',
            f'Environment={quote_systemd(f"DEV_MAX_TURNS={worker.max_turns}")}',
            f'Environment={quote_systemd(f"DEV_WORKDIR={worker.workdir or str(Path.home())}")}',
            f'Environment={quote_systemd(f"DEV_PROFILE_HOME={profile_home}")}',
            f'Environment={quote_systemd(f"DEV_ENGINE_ROOT={engine_root}")}',
            f'Environment={quote_systemd(f"PYTHONPATH={engine_root}")}',
            f'Environment={quote_systemd(f"HERMES_HOME={profile_home}")}',
        ]
        if worker.target_host:
            env_lines.append(f'Environment={quote_systemd(f"DEV_TARGET_HOST={worker.target_host}")}')
        if worker.duty:
            env_lines.append(f'Environment={quote_systemd(f"DEV_EXPERT_DUTY={worker.duty}")}')

        # Profile level and worker level environment variables
        combined_env = dict(self.env)
        combined_env.update(worker.env)
        for k, v in sorted(combined_env.items()):
            env_lines.append(f'Environment={quote_systemd(f"{k}={v}")}')

        env_block = "\n".join(env_lines)
        exec_start = (
            f"{self.python_bin} -m agentcore.pool "
            f"--root {queue_root} worker "
            f"--runner-config {runner_config} "
            f"--workers {worker.concurrency} "
            f"--timeout {worker.timeout} "
            f"--max-file {worker.max_file} "
            f"--backend stdlib"
        )

        return (
            f"[Unit]\n"
            f"Description=Super Agent worker [{worker.role}] for {self.profile_id}\n"
            f"After=network.target\n"
            f"\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"UMask=0077\n"
            f"{env_block}\n"
            f"ExecStart={exec_start}\n"
            f"Restart=on-failure\n"
            f"RestartSec=5\n"
            f"KillMode=control-group\n"
            f"TimeoutStopSec=30\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=default.target\n"
        )

    def render_hooks_service(self) -> str:
        if not self.proactive_hooks:
            raise ValueError("Proactive hooks not defined in this profile")
        profile_home = str(self.get_profile_home())
        engine_root = str(self.get_engine_root())
        config_path = str(self.resolve_path(self.proactive_hooks.config_file))
        db_path = str(
            self.resolve_path(
                self.proactive_hooks.db,
                self.get_profile_home() / "state" / "autotask" / "proactive_hooks.db"
            )
        )
        liveness_db = str(
            self.resolve_path(
                self.proactive_hooks.liveness_db,
                Path.home() / ".local" / "state" / "agentcore" / "liveness.db"
            )
        )

        exec_start = (
            f"{self.python_bin} -m agentcore.proactive_hooks "
            f"--config {config_path} "
            f"--db {db_path} "
            f"--liveness-db {liveness_db}"
        )

        return (
            f"[Unit]\n"
            f"Description=Super Agent proactive hooks runner for {self.profile_id}\n"
            f"After=network-online.target\n"
            f"\n"
            f"[Service]\n"
            f"Type=oneshot\n"
            f"UMask=0077\n"
            f"WorkingDirectory={engine_root}\n"
            f'Environment={quote_systemd(f"PYTHONPATH={engine_root}")}\n'
            f'Environment={quote_systemd(f"HERMES_HOME={profile_home}")}\n'
            f"ExecStart={exec_start}\n"
        )

    def render_hooks_timer(self) -> str:
        if not self.proactive_hooks:
            raise ValueError("Proactive hooks not defined in this profile")
        schedule = self.proactive_hooks.timer_schedule
        return (
            f"[Unit]\n"
            f"Description=Super Agent proactive hooks timer for {self.profile_id}\n"
            f"\n"
            f"[Timer]\n"
            f"OnCalendar={schedule}\n"
            f"Persistent=true\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=timers.target\n"
        )

    def render_memory_service(self) -> str:
        if not self.memory:
            raise ValueError("Memory not defined in this profile")
        profile_home = str(self.get_profile_home())
        engine_root = str(self.get_engine_root())
        mem_source = str(self.resolve_path(self.memory.source))
        user_principal = self.memory.user
        agent_principal = self.memory.agent

        exec_start = (
            f"{self.python_bin} -m agentcore.memory.document_watcher "
            f"--bootstrap tools.memory_runtime_profile:configure "
            f"--sync-hook tools.curated_memory_profile:sync_profile_document "
            f"--source {mem_source} "
            f"--user {user_principal} "
            f"--agent {agent_principal}"
        )

        return (
            f"[Unit]\n"
            f"Description=Super Agent memory watcher for {self.profile_id}\n"
            f"After=network.target\n"
            f"\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"UMask=0077\n"
            f'Environment={quote_systemd(f"PYTHONPATH={engine_root}")}\n'
            f'Environment={quote_systemd(f"HERMES_HOME={profile_home}")}\n'
            f"ExecStart={exec_start}\n"
            f"Restart=on-failure\n"
            f"RestartSec=10\n"
            f"\n"
            f"[Install]\n"
            f"WantedBy=default.target\n"
        )

    def render_all_units(self) -> Dict[str, str]:
        """Generate all systemd unit definitions for this profile."""
        units: Dict[str, str] = {}
        for worker in self.workers:
            units[self.worker_unit_name(worker.role)] = self.render_worker_service(worker)
        if self.proactive_hooks:
            units[self.hooks_unit_name()] = self.render_hooks_service()
            units[self.hooks_timer_name()] = self.render_hooks_timer()
        if self.memory:
            units[self.memory_unit_name()] = self.render_memory_service()
        return units

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "profile_id": self.profile_id,
            "description": self.description,
            "version": self.version,
        }
        if self.managed_root:
            d["managed_root"] = self.managed_root
        if self.profile_home:
            d["profile_home"] = self.profile_home
        if self.engine_root:
            d["engine_root"] = self.engine_root
        if self.python_bin != sys.executable:
            d["python_bin"] = self.python_bin
        if self.gateway:
            d["gateway"] = {"port": self.gateway.port, "service_name": self.gateway.service_name}
        if self.env:
            d["env"] = dict(self.env)
        d["workers"] = [
            {
                "role": w.role,
                "model_tier": w.model_tier,
                "workdir": w.workdir,
                "duty": w.duty,
                "target_host": w.target_host,
                "concurrency": w.concurrency,
                "timeout": w.timeout,
                "max_file": w.max_file,
                "max_turns": w.max_turns,
                "run_budget": w.run_budget,
                "env": w.env,
            }
            for w in self.workers
        ]
        if self.proactive_hooks:
            d["proactive_hooks"] = {
                "config_file": self.proactive_hooks.config_file,
                "timer_schedule": self.proactive_hooks.timer_schedule,
                "db": self.proactive_hooks.db,
                "liveness_db": self.proactive_hooks.liveness_db,
            }
        if self.memory:
            d["memory"] = {
                "source": self.memory.source,
                "user": self.memory.user,
                "agent": self.memory.agent,
            }
        return d


def load_profile_spec(data: Dict[str, Any], base_dir: Optional[Path] = None) -> BusinessProfileSpec:
    """Construct and validate a BusinessProfileSpec from a dictionary."""
    profile_id = str(data.get("profile_id") or "").strip()
    description = str(data.get("description") or "").strip()
    version = str(data.get("version") or "1.0.0").strip()
    managed_root = data.get("managed_root")
    profile_home = data.get("profile_home")
    engine_root = data.get("engine_root")
    python_bin = data.get("python_bin") or sys.executable
    env = dict(data.get("env") or {})

    gateway_spec: Optional[GatewaySpec] = None
    if "gateway" in data and isinstance(data["gateway"], dict):
        gw = data["gateway"]
        gateway_spec = GatewaySpec(port=int(gw.get("port", 8080)), service_name=gw.get("service_name"))

    workers: List[WorkerSpec] = []
    for w in data.get("workers") or []:
        if isinstance(w, dict) and "role" in w:
            workers.append(
                WorkerSpec(
                    role=str(w["role"]),
                    model_tier=w.get("model_tier"),
                    workdir=str(w.get("workdir") or ""),
                    duty=w.get("duty"),
                    target_host=w.get("target_host"),
                    concurrency=int(w.get("concurrency", 1)),
                    timeout=int(w.get("timeout", 600)),
                    max_file=int(w.get("max_file", 67108864)),
                    max_turns=int(w.get("max_turns", 40)),
                    run_budget=int(w.get("run_budget", 480)),
                    env=dict(w.get("env") or {}),
                )
            )

    proactive_hooks_spec: Optional[ProactiveHooksSpec] = None
    if "proactive_hooks" in data and isinstance(data["proactive_hooks"], dict):
        ph = data["proactive_hooks"]
        proactive_hooks_spec = ProactiveHooksSpec(
            config_file=str(ph.get("config_file") or ""),
            timer_schedule=str(ph.get("timer_schedule") or "*:00/30"),
            db=ph.get("db"),
            liveness_db=ph.get("liveness_db"),
        )

    memory_spec: Optional[MemorySpec] = None
    if "memory" in data and isinstance(data["memory"], dict):
        mem = data["memory"]
        memory_spec = MemorySpec(
            source=str(mem.get("source") or ""),
            user=str(mem.get("user") or "operator"),
            agent=str(mem.get("agent") or "expert"),
        )

    spec = BusinessProfileSpec(
        profile_id=profile_id,
        description=description,
        version=version,
        managed_root=managed_root,
        profile_home=profile_home,
        engine_root=engine_root,
        python_bin=python_bin,
        gateway=gateway_spec,
        workers=workers,
        proactive_hooks=proactive_hooks_spec,
        memory=memory_spec,
        env=env,
        base_dir=base_dir,
    )

    errors = spec.validate()
    if errors:
        raise ValueError("Profile specification validation errors:\n  - " + "\n  - ".join(errors))
    return spec


def load_profile_file(path: str | Path) -> BusinessProfileSpec:
    """Load and parse a profile specification from a YAML or JSON file."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Profile file not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
        except ImportError:
            # Fallback simple json if PyYAML is not installed
            try:
                data = json.loads(text)
            except Exception as e:
                raise ImportError("PyYAML is required to parse .yaml profiles: pip install PyYAML") from e
    else:
        data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError(f"Profile content must be a dictionary/object, got {type(data).__name__}")
    return load_profile_spec(data, base_dir=p.parent)


def package_profile_bundle(spec_file: str | Path, output_tar: str | Path) -> Path:
    """Package a profile specification and referenced local files into a portable tar.gz bundle."""
    spec = load_profile_file(spec_file)
    spec_path = Path(spec_file).resolve()
    out_path = Path(output_tar).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base_dir = spec_path.parent
    files_to_pack: List[Tuple[Path, str]] = [(spec_path, "profile.yaml")]

    # Check and pack proactive hooks config if relative to base_dir
    if spec.proactive_hooks and spec.proactive_hooks.config_file:
        ph_path = spec.resolve_path(spec.proactive_hooks.config_file)
        if ph_path.is_file() and ph_path.is_relative_to(base_dir):
            rel = ph_path.relative_to(base_dir).as_posix()
            files_to_pack.append((ph_path, rel))

    # Check and pack memory source if relative to base_dir
    if spec.memory and spec.memory.source:
        mem_path = spec.resolve_path(spec.memory.source)
        if mem_path.is_file() and mem_path.is_relative_to(base_dir):
            rel = mem_path.relative_to(base_dir).as_posix()
            files_to_pack.append((mem_path, rel))

    with tarfile.open(out_path, "w:gz") as tar:
        for local_file, archive_name in files_to_pack:
            tar.add(local_file, arcname=archive_name)

    return out_path
