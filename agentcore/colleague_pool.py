"""Homogeneous Colleague Pool with FlowCoro-inspired scheduling primitives.

Worker compute slots are completely homogeneous and dynamic:
Any slot can morph into any colleague role (e.g., architect, cases, reviewer,
test, triage, backend, frontend, ops, or custom roles) upon acquiring a task.
Scheduling primitives enforce bounded backpressure, event-driven reaping (no busy wait),
slot leasing, attempt fencing with epoch tokens, and isolated workspaces to prevent
file stampede under high concurrency (e.g., 100+ concurrent slots).
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union
import uuid

from agentcore.colleague_dev import DEV_ROLES, get_dev_roles, get_role_contract_markdown
from agentcore.scheduler import AgentScheduler, CancellationToken, ToolHandle

logger = logging.getLogger(__name__)


class SlotState:
    IDLE = "idle"
    ACQUIRED = "acquired"
    MORPHED = "morphed"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASING = "releasing"


@dataclasses.dataclass(frozen=True)
class RoleSpec:
    """Standardized role specification for a morphed colleague."""
    role_id: str
    display_name: str
    duty: str
    can_do: Tuple[str, ...]
    boundaries: Tuple[str, ...]
    contract_markdown: str


class RoleRegistry:
    """Registry and prompt overlay generator for dynamic colleague roles."""

    def __init__(self, custom_roles: Optional[Mapping[str, Mapping[str, Any]]] = None):
        self._roles: Dict[str, RoleSpec] = {}
        # Load canonical development roles by default
        for role_id, meta in get_dev_roles().items():
            self.register_role(
                role_id=role_id,
                display_name=meta.get("display_name", role_id),
                duty=meta.get("duty", ""),
                can_do=tuple(meta.get("can_do", ())),
                boundaries=tuple(meta.get("boundaries", ())),
                contract_markdown=get_role_contract_markdown(role_id),
            )
        if custom_roles:
            for role_id, meta in custom_roles.items():
                self.register_role(
                    role_id=role_id,
                    display_name=meta.get("display_name", role_id),
                    duty=meta.get("duty", ""),
                    can_do=tuple(meta.get("can_do", ())),
                    boundaries=tuple(meta.get("boundaries", ())),
                    contract_markdown=meta.get("contract_markdown") or self._make_contract(role_id, meta),
                )

    @staticmethod
    def _make_contract(role_id: str, meta: Mapping[str, Any]) -> str:
        can_do_lines = "\n".join(f"- {c}" for c in meta.get("can_do", ()))
        boundaries_lines = "\n".join(f"- {b}" for b in meta.get("boundaries", ()))
        return (
            f"# Role Contract: {meta.get('display_name', role_id)} ({role_id})\n\n"
            f"## Duty\n{meta.get('duty', '')}\n\n"
            f"## Capabilities & Authorizations\n{can_do_lines}\n\n"
            f"## Boundaries & Discipline\n{boundaries_lines}\n"
        )

    def register_role(
        self,
        role_id: str,
        display_name: str,
        duty: str,
        can_do: Sequence[str],
        boundaries: Sequence[str],
        contract_markdown: str,
    ) -> None:
        self._roles[role_id] = RoleSpec(
            role_id=role_id,
            display_name=display_name,
            duty=duty,
            can_do=tuple(can_do),
            boundaries=tuple(boundaries),
            contract_markdown=contract_markdown,
        )

    def get_role(self, role_id: str) -> RoleSpec:
        if role_id not in self._roles:
            # Fallback to dynamic generic role spec
            return RoleSpec(
                role_id=role_id,
                display_name=f"Dynamic Colleague ({role_id})",
                duty="Execute dynamic domain task within assigned bounds",
                can_do=("Execute assigned operations within worktree",),
                boundaries=("Respect isolation and deliver physical evidence",),
                contract_markdown=f"# Dynamic Role: {role_id}\n\nExecute assigned task responsibly.\n",
            )
        return self._roles[role_id]

    def list_roles(self) -> List[str]:
        return sorted(self._roles.keys())

    def render_prompt_overlay(self, role_id: str, goal: str, context: Optional[Mapping[str, Any]] = None) -> str:
        spec = self.get_role(role_id)
        overlay = [
            f"=== ROLE ASSIGNMENT: {spec.display_name} ({spec.role_id}) ===",
            spec.contract_markdown,
            "=== ACTIVE OBJECTIVE ===",
            f"Goal: {goal}",
        ]
        if context:
            overlay.append("=== CONTEXT & PARAMETERS ===")
            overlay.append(json.dumps(dict(context), indent=2, ensure_ascii=False))
        return "\n\n".join(overlay)


class ToolRegistry:
    """Manages available tools and maps role boundaries to tool access."""

    def __init__(self, base_tools: Optional[Mapping[str, Callable]] = None):
        self._tools: Dict[str, Callable] = dict(base_tools or {})
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        if "read_file" not in self._tools:
            def _read_file(worktree: Path, path: str) -> str:
                target = (worktree / path).resolve()
                if not target.is_relative_to(worktree.resolve()):
                    raise PermissionError("path_traversal_denied")
                return target.read_text(encoding="utf-8")
            self._tools["read_file"] = _read_file

        if "write_file" not in self._tools:
            def _write_file(worktree: Path, path: str, content: str) -> str:
                target = (worktree / path).resolve()
                if not target.is_relative_to(worktree.resolve()):
                    raise PermissionError("path_traversal_denied")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_text(content, encoding="utf-8")
                return f"wrote {len(content)} bytes to {path}"
            self._tools["write_file"] = _write_file

        if "list_dir" not in self._tools:
            def _list_dir(worktree: Path, path: str = ".") -> List[str]:
                target = (worktree / path).resolve()
                if not target.is_relative_to(worktree.resolve()):
                    raise PermissionError("path_traversal_denied")
                if not target.is_dir():
                    return []
                return [p.name for p in target.iterdir()]
            self._tools["list_dir"] = _list_dir

    def register_tool(self, name: str, fn: Callable) -> None:
        self._tools[name] = fn

    def get_tools_for_role(self, role_spec: RoleSpec, worktree: Path) -> Dict[str, Callable]:
        """Bind worktree to available tools matching role permissions."""
        bound: Dict[str, Callable] = {}
        for name, fn in self._tools.items():
            # Bind worktree if first parameter is worktree
            import inspect
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            if params and params[0] in ("worktree", "scratch_dir", "cwd"):
                def _wrapper(fn=fn, *args, **kwargs):
                    return fn(worktree, *args, **kwargs)
                bound[name] = _wrapper
            else:
                bound[name] = fn
        return bound


@dataclasses.dataclass
class MorphedColleagueContext:
    """Execution context of a homogeneous slot morphed into a specific colleague role."""
    slot_id: int
    role: str
    role_spec: RoleSpec
    prompt_overlay: str
    tools: Dict[str, Callable]
    scratch_dir: Path
    task_id: str
    attempt_token: str
    epoch: int
    lease_deadline: float
    cancel_token: Optional[CancellationToken] = None
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)
    _modified_files: Set[str] = dataclasses.field(default_factory=set)
    pool: Optional[Any] = None

    def is_cancelled(self) -> bool:
        if self.cancel_token:
            return self.cancel_token.is_cancelled()
        return False

    def throw_if_cancelled(self) -> None:
        if self.cancel_token:
            self.cancel_token.throw_if_cancellation_requested()

    def renew_lease(self, extend_seconds: Optional[float] = None) -> float:
        """Renew the slot lease on both in-memory slot and durable pool if available."""
        if self.pool is not None and hasattr(self.pool, "renew_lease"):
            self.lease_deadline = self.pool.renew_lease(self.slot_id, extend_seconds)
            return self.lease_deadline
        self.lease_deadline = time.monotonic() + (extend_seconds or 60.0)
        return self.lease_deadline

    def write_file(self, relpath: str, content: str) -> Path:
        p = Path(relpath)
        if p.is_absolute():
            target = p.resolve()
        else:
            target = (self.scratch_dir / p).resolve()
        if not target.is_relative_to(self.scratch_dir.resolve()):
            raise PermissionError("worktree_escape_denied")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(content, encoding="utf-8")
        clean_rel = str(target.relative_to(self.scratch_dir.resolve()))
        self._modified_files.add(clean_rel)
        return target

    def read_file(self, relpath: str) -> str:
        p = Path(relpath)
        if p.is_absolute():
            target = p.resolve()
        else:
            target = (self.scratch_dir / p).resolve()
        if not target.is_relative_to(self.scratch_dir.resolve()):
            raise PermissionError("worktree_escape_denied")
        return target.read_text(encoding="utf-8")

    def execute_tool(self, name: str, *args, **kwargs) -> Any:
        self.throw_if_cancelled()
        if name not in self.tools:
            raise KeyError(f"tool_not_found: {name}")
        return self.tools[name](*args, **kwargs)


class ComputeSlot:
    """A homogeneous compute worker slot.
    
    Any slot can dynamically morph into any colleague role and demorph back to idle.
    """

    def __init__(self, slot_id: int, base_scratch_root: Path):
        self.slot_id = slot_id
        self.base_scratch_root = base_scratch_root
        self.state = SlotState.IDLE
        self.context: Optional[MorphedColleagueContext] = None
        self._lock = threading.Lock()

    def morph(
        self,
        role: str,
        task_id: str,
        attempt_token: str,
        epoch: int,
        goal: str,
        role_registry: RoleRegistry,
        tool_registry: ToolRegistry,
        lease_duration: float,
        cancel_token: Optional[CancellationToken] = None,
        context_data: Optional[Mapping[str, Any]] = None,
        pool: Optional[Any] = None,
    ) -> MorphedColleagueContext:
        with self._lock:
            if self.state not in (SlotState.IDLE, SlotState.ACQUIRED):
                raise RuntimeError(f"cannot_morph_slot_in_state: {self.state}")

            role_spec = role_registry.get_role(role)
            prompt_overlay = role_registry.render_prompt_overlay(role, goal, context_data)
            
            # Isolated worktree per slot and attempt token to eliminate file stampede
            scratch_dir = self.base_scratch_root / f"slot_{self.slot_id}" / attempt_token
            scratch_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

            tools = tool_registry.get_tools_for_role(role_spec, scratch_dir)
            lease_deadline = time.monotonic() + lease_duration

            self.context = MorphedColleagueContext(
                slot_id=self.slot_id,
                role=role,
                role_spec=role_spec,
                prompt_overlay=prompt_overlay,
                tools=tools,
                scratch_dir=scratch_dir,
                task_id=task_id,
                attempt_token=attempt_token,
                epoch=epoch,
                lease_deadline=lease_deadline,
                cancel_token=cancel_token,
                metadata=dict(context_data or {}),
                pool=pool,
            )
            self.state = SlotState.MORPHED
            return self.context

    def renew_lease(self, extend_seconds: float) -> float:
        with self._lock:
            if not self.context:
                raise RuntimeError("no_active_context_to_renew")
            self.context.lease_deadline = time.monotonic() + extend_seconds
            return self.context.lease_deadline

    def produce_evidence(
        self,
        summary: str,
        success: bool = True,
        artifacts_dir: Optional[Path] = None,
        extra_manifest: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Produce structured verification artifacts conforming to dev_traceability_v1."""
        with self._lock:
            if not self.context:
                raise RuntimeError("cannot_produce_evidence_without_context")
            
            ctx = self.context
            target_dir = artifacts_dir or ctx.scratch_dir
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

            report_data = {
                "job": ctx.task_id,
                "attempt": ctx.attempt_token,
                "verification": "dev_traceability_v1",
                "role": ctx.role,
                "summary": summary,
                "success": bool(success),
                "timestamp": time.time(),
            }
            report_raw = json.dumps(report_data, sort_keys=True, indent=2, ensure_ascii=False)
            (target_dir / "report.json").write_text(report_raw, encoding="utf-8")

            artifacts = sorted(list({"report.json", "manifest.json"} | (set(extra_manifest.get("artifacts", [])) if extra_manifest else set())))
            manifest_data = {
                "job": ctx.task_id,
                "attempt": ctx.attempt_token,
                "verification": "dev_traceability_v1",
                "worktree": str(ctx.scratch_dir),
                "modified_files": sorted(list(ctx._modified_files)),
                "artifacts": artifacts,
            }
            if extra_manifest:
                manifest_data.update(extra_manifest)
            manifest_data["artifacts"] = artifacts
            manifest_raw = json.dumps(manifest_data, sort_keys=True, indent=2, ensure_ascii=False)
            (target_dir / "manifest.json").write_text(manifest_raw, encoding="utf-8")

            return {
                "job": ctx.task_id,
                "attempt": ctx.attempt_token,
                "role": ctx.role,
                "verification": "dev_traceability_v1",
                "summary": summary,
                "success": bool(success),
                "report_sha256": hashlib.sha256(report_raw.encode("utf-8")).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_raw.encode("utf-8")).hexdigest(),
                "worktree": str(ctx.scratch_dir),
                "modified_files": sorted(list(ctx._modified_files)),
                "artifacts": artifacts,
            }

    def demorph(self, cleanup_scratch: bool = False) -> None:
        with self._lock:
            self.state = SlotState.RELEASING
            if cleanup_scratch and self.context and self.context.scratch_dir.is_dir():
                shutil.rmtree(self.context.scratch_dir, ignore_errors=True)
            self.context = None
            self.state = SlotState.IDLE


class ColleaguePool:
    """Homogeneous Colleague Pool orchestrating dynamic compute slots.
    
    Adopts FlowCoro's core scheduling primitives:
    - Bounded backpressure (capacity limits, no unbounded thread creation).
    - Event-driven reaping via condition variables (zero busy-waiting / CPU spin).
    - Slot leasing and renewal.
    - Attempt fencing / epoch tokens to prevent stale overwrites.
    - Full separation of generic scheduling primitives from role domain logic.
    - High-concurrency scalability (up to 100+ parallel workers) with isolated worktrees.
    """

    def __init__(
        self,
        pool: Optional[Any] = None,
        *,
        max_workers: int = 128,
        capacity: Optional[int] = None,
        backend: str = "stdlib",
        scratch_root: Optional[Union[str, Path]] = None,
        lease_duration: float = 60.0,
        role_registry: Optional[RoleRegistry] = None,
        tool_registry: Optional[ToolRegistry] = None,
    ):
        if not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if capacity is not None and (not isinstance(capacity, int) or capacity < max_workers):
            raise ValueError("capacity must be an integer >= max_workers")
        if lease_duration <= 0:
            raise ValueError("lease_duration must be positive")

        self.durable_pool = pool
        self.max_workers = max_workers
        self.queue_capacity = capacity or (max_workers * 2)
        self.backend = backend
        self.lease_duration = lease_duration
        self.role_registry = role_registry or RoleRegistry()
        self.tool_registry = tool_registry or ToolRegistry()

        if scratch_root is not None:
            self.scratch_root = Path(scratch_root).resolve()
        elif pool is not None and hasattr(pool, "root"):
            self.scratch_root = (Path(pool.root) / "scratch").resolve()
        else:
            self.scratch_root = Path(tempfile.gettempdir()) / f"colleague_pool_{uuid.uuid4().hex[:8]}"
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)

        self._cv = threading.Condition()
        self._closed = False
        self._slots: List[ComputeSlot] = [
            ComputeSlot(slot_id=i, base_scratch_root=self.scratch_root)
            for i in range(max_workers)
        ]
        self._idle_slot_ids: Set[int] = set(range(max_workers))
        self._active_slots: Dict[int, ComputeSlot] = {}
        
        # Epoch fencing map: task_id -> current epoch token
        self._task_epochs: Dict[str, int] = {}
        
        # Task results store: task_id -> result
        self._task_results: Dict[str, Dict[str, Any]] = {}
        self._completed_count = 0
        self._failed_count = 0

        # Backing execution scheduler with bounded queue capacity
        self._scheduler = AgentScheduler(backend=backend, workers=max_workers, capacity=self.queue_capacity)

    @property
    def capacity(self) -> int:
        return self.queue_capacity

    @property
    def max_concurrent(self) -> int:
        return self.max_workers

    @property
    def active_count(self) -> int:
        with self._cv:
            return len(self._active_slots)

    @property
    def idle_count(self) -> int:
        with self._cv:
            return len(self._idle_slot_ids)

    @property
    def completed_count(self) -> int:
        with self._cv:
            return self._completed_count

    @property
    def failed_count(self) -> int:
        with self._cv:
            return self._failed_count

    def get_slot_status(self) -> List[Dict[str, Any]]:
        """Return snapshot of all compute slots and their active morph states."""
        with self._cv:
            statuses = []
            for slot in self._slots:
                ctx = slot.context
                statuses.append({
                    "slot_id": slot.slot_id,
                    "state": slot.state,
                    "role": ctx.role if ctx else None,
                    "task_id": ctx.task_id if ctx else None,
                    "attempt_token": ctx.attempt_token if ctx else None,
                    "epoch": ctx.epoch if ctx else None,
                })
            return statuses

    def acquire_slot(
        self,
        role: str,
        task_id: str,
        goal: str,
        timeout: Optional[float] = None,
        context_data: Optional[Mapping[str, Any]] = None,
        attempt_token: Optional[str] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> MorphedColleagueContext:
        """Acquire an idle homogeneous slot with bounded backpressure and morph into role."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be nonnegative")

        deadline = None if timeout is None else time.monotonic() + timeout

        with self._cv:
            while not self._idle_slot_ids and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("colleague_pool_capacity_exhausted")
                self._cv.wait(remaining)

            if self._closed:
                raise RuntimeError("colleague_pool_is_closed")

            slot_id = self._idle_slot_ids.pop()
            slot = self._slots[slot_id]
            slot.state = SlotState.ACQUIRED
            self._active_slots[slot_id] = slot

            # Monotonic epoch generation for attempt fencing
            current_epoch = self._task_epochs.get(task_id, 0) + 1
            self._task_epochs[task_id] = current_epoch

            token = attempt_token or uuid.uuid4().hex

            try:
                context = slot.morph(
                    role=role,
                    task_id=task_id,
                    attempt_token=token,
                    epoch=current_epoch,
                    goal=goal,
                    role_registry=self.role_registry,
                    tool_registry=self.tool_registry,
                    lease_duration=self.lease_duration,
                    cancel_token=cancel_token,
                    context_data=context_data,
                    pool=self,
                )
                return context
            except BaseException:
                slot.demorph(cleanup_scratch=True)
                self._active_slots.pop(slot_id, None)
                self._idle_slot_ids.add(slot_id)
                self._cv.notify()
                raise

    def release_slot(self, slot_id: int, cleanup_scratch: bool = False) -> None:
        """Release a compute slot, demorphing it back to homogeneous idle state."""
        with self._cv:
            slot = self._active_slots.pop(slot_id, None)
            if slot is not None:
                slot.demorph(cleanup_scratch=cleanup_scratch)
                self._idle_slot_ids.add(slot_id)
                self._cv.notify_all()

    def renew_lease(self, slot_id: int, extend_seconds: Optional[float] = None) -> float:
        """Renew slot lease to prevent premature reclamation during long operations."""
        extend = extend_seconds or self.lease_duration
        with self._cv:
            slot = self._active_slots.get(slot_id)
            if slot is None:
                raise KeyError(f"slot_{slot_id}_not_active")
            new_deadline = slot.renew_lease(extend)
            # If connected to durable pool, heartbeat the attempt
            if self.durable_pool is not None and slot.context:
                with contextlib.suppress(Exception):
                    with self.durable_pool.db() as db:
                        db.execute(
                            "UPDATE jobs SET heartbeat=? WHERE id=? AND attempt=?",
                            (time.time(), slot.context.task_id, slot.context.attempt_token),
                        )
            return new_deadline

    def commit_task_result(
        self,
        task_id: str,
        epoch: int,
        attempt_token: str,
        result: Dict[str, Any],
        status: str = "completed",
    ) -> bool:
        """Commit task result with attempt fencing (epoch token validation)."""
        with self._cv:
            expected_epoch = self._task_epochs.get(task_id)
            if expected_epoch != epoch:
                logger.warning(
                    "Attempt fencing rejected stale commit for task %s: current epoch %s != commit epoch %s",
                    task_id, expected_epoch, epoch,
                )
                return False

            self._task_results[task_id] = {
                "status": status,
                "epoch": epoch,
                "attempt": attempt_token,
                "result": result,
                "committed_at": time.time(),
            }

            # If durable pool is attached, record terminal status with attempt fence
            if self.durable_pool is not None:
                try:
                    with self.durable_pool.db() as db:
                        receipt_payload = {
                            "ok": (status == "completed"),
                            "result": {
                                "job": task_id,
                                "attempt": attempt_token,
                                "verification": "dev_traceability_v1",
                                "summary": result.get("evidence", {}).get("summary", ""),
                                "success": (status == "completed"),
                                "role": result.get("role"),
                                "output": result.get("output"),
                                "worktree": result.get("evidence", {}).get("worktree"),
                                "artifacts": result.get("evidence", {}).get("artifacts", ["report.json", "manifest.json"]),
                            },
                        }
                        updated = self.durable_pool._terminal(
                            db, task_id, status, receipt_payload, attempt=attempt_token
                        )
                        if not updated:
                            logger.warning(
                                "Attempt fencing: durable pool rejected terminal update for %s attempt %s",
                                task_id, attempt_token,
                            )
                            return False
                except Exception as exc:
                    logger.error("Durable pool terminal commit failed for task %s: %s", task_id, exc)
                    return False

            return True

    def submit_colleague_task(
        self,
        role: str,
        goal: str,
        handler: Optional[Callable[[MorphedColleagueContext], Any]] = None,
        task_id: Optional[str] = None,
        attempt_token: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        admission_timeout: Optional[float] = None,
        claim_lock_fd: Optional[int] = None,
        cleanup_scratch: bool = False,
    ) -> ToolHandle:
        """Asynchronously dispatch a task into the colleague pool with bounded backpressure."""
        tid = task_id or uuid.uuid4().hex
        adm_timeout = admission_timeout if admission_timeout is not None else timeout
        
        def _execute_in_slot(cancel_token: Optional[CancellationToken] = None) -> Dict[str, Any]:
            context = self.acquire_slot(
                role=role,
                task_id=tid,
                goal=goal,
                timeout=timeout,
                context_data=payload,
                attempt_token=attempt_token,
                cancel_token=cancel_token,
            )
            slot = self._slots[context.slot_id]
            slot.state = SlotState.EXECUTING
            start_time = time.monotonic()
            
            try:
                if handler is not None:
                    user_output = handler(context)
                else:
                    user_output = {"executed_role": role, "goal": goal}

                # Produce verified evidence
                summary = (
                    f"Colleague {context.role_spec.display_name} ({role}) "
                    f"successfully executed goal: {goal}"
                )
                evidence = slot.produce_evidence(
                    summary=summary,
                    success=True,
                    artifacts_dir=self._get_attempts_dir(context.attempt_token),
                )
                final_result = {
                    "ok": True,
                    "task_id": tid,
                    "attempt": context.attempt_token,
                    "role": role,
                    "output": user_output,
                    "evidence": evidence,
                    "duration_s": time.monotonic() - start_time,
                }
                slot.state = SlotState.COMPLETED
                self.commit_task_result(tid, context.epoch, context.attempt_token, final_result, status="completed")
                with self._cv:
                    self._completed_count += 1
                return final_result
            except BaseException as exc:
                slot.state = SlotState.FAILED
                summary = f"Colleague execution failed: {exc}"
                evidence = slot.produce_evidence(
                    summary=summary,
                    success=False,
                    artifacts_dir=self._get_attempts_dir(context.attempt_token),
                )
                fail_result = {
                    "ok": False,
                    "task_id": tid,
                    "attempt": context.attempt_token,
                    "role": role,
                    "error": str(exc),
                    "evidence": evidence,
                    "duration_s": time.monotonic() - start_time,
                }
                self.commit_task_result(tid, context.epoch, context.attempt_token, fail_result, status="failed")
                with self._cv:
                    self._failed_count += 1
                raise
            finally:
                if claim_lock_fd is not None:
                    try:
                        os.close(claim_lock_fd)
                    except OSError:
                        pass
                self.release_slot(context.slot_id, cleanup_scratch=cleanup_scratch)

        try:
            handle = self._scheduler.submit(
                f"colleague-{role}-{tid[:8]}",
                _execute_in_slot,
                admission_timeout=adm_timeout,
            )
        except BaseException:
            if claim_lock_fd is not None:
                try:
                    os.close(claim_lock_fd)
                except OSError:
                    pass
            raise

        def _notify_done(_):
            with self._cv:
                self._cv.notify_all()

        handle.future.add_done_callback(_notify_done)
        return handle

    def _get_attempts_dir(self, attempt_token: str) -> Optional[Path]:
        if self.durable_pool is not None and hasattr(self.durable_pool, "root"):
            p = Path(self.durable_pool.root) / "attempts" / attempt_token
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            return p
        return None

    def run_task(
        self,
        role: str,
        goal: str,
        handler: Optional[Callable[[MorphedColleagueContext], Any]] = None,
        task_id: Optional[str] = None,
        attempt_token: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        cleanup_scratch: bool = False,
    ) -> Dict[str, Any]:
        """Synchronously execute a colleague task through the pool and return the verified receipt."""
        handle = self.submit_colleague_task(
            role=role,
            goal=goal,
            handler=handler,
            task_id=task_id,
            attempt_token=attempt_token,
            payload=payload,
            timeout=timeout,
            cleanup_scratch=cleanup_scratch,
        )
        return handle.result(timeout=timeout)

    def dispatch_from_durable_pool(
        self,
        handler: Optional[Callable[[MorphedColleagueContext], Any]] = None,
        batch_size: int = 32,
        stop_event: Optional[threading.Event] = None,
        drain: bool = False,
        cleanup_scratch: bool = False,
    ) -> Dict[str, int]:
        """Event-driven dispatcher consuming from durable pool in coordinated batches.
        
        Prevents SQLite lock stampede across concurrent workers by coordinating claims.
        """
        if self.durable_pool is None:
            raise RuntimeError("durable_pool_required_for_dispatch")

        stop = stop_event or threading.Event()
        stats = {"claimed": 0, "dispatched": 0}
        active_handles: List[ToolHandle] = []

        while not stop.is_set():
            # Claim up to available idle slots
            available = self.idle_count
            claim_count = min(available, batch_size)
            
            if claim_count > 0:
                for _ in range(claim_count):
                    if stop.is_set():
                        break
                    claim = self.durable_pool.claim(lease=self.lease_duration)
                    if claim is None:
                        break
                    row, fd = claim
                    stats["claimed"] += 1
                    
                    try:
                        payload = json.loads(row["payload"])
                    except Exception:
                        payload = {}
                    
                    role = payload.get("role") or payload.get("member_id") or "cases"
                    goal = payload.get("goal") or payload.get("prompt") or f"Execute task {row['id']}"

                    try:
                        handle = self.submit_colleague_task(
                            role=role,
                            goal=goal,
                            handler=handler,
                            task_id=row["id"],
                            attempt_token=row["attempt"],
                            payload=payload,
                            cleanup_scratch=cleanup_scratch,
                            claim_lock_fd=fd,
                        )
                        active_handles.append(handle)
                        stats["dispatched"] += 1
                    except BaseException:
                        os.close(fd)
                        raise

            # Event-driven reaping of active handles
            active_handles = [h for h in active_handles if not h.done()]
            
            if not active_handles and drain:
                break

            with self._cv:
                if not active_handles and not stop.is_set():
                    # Wait for notifications instead of busy spinning
                    self._cv.wait(timeout=0.05)
                elif active_handles:
                    # Wait for any completion
                    self._cv.wait(timeout=0.02)

        # Wait for all dispatched tasks if draining
        if drain:
            for h in list(active_handles):
                h.result()

        return stats

    def drain(self, timeout: Optional[float] = None) -> None:
        """Event-driven wait until all active compute slots are idle and scheduler has no pending tasks."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while (self._active_slots or self._scheduler.pending_count > 0) and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("drain_timed_out")
                self._cv.wait(remaining)

    def close(self, cancel_pending: bool = True) -> None:
        """Close the pool and cleanly shutdown the backing scheduler."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        self._scheduler.close(cancel_pending=cancel_pending)

    def __enter__(self) -> "ColleaguePool":
        return self

    def __exit__(self, *args) -> None:
        self.close()
