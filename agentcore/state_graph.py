"""agentcore.state_graph — Deterministic state graph with durable checkpointing.

Inspired by LangGraph's declarative execution model, this pure-Python engine provides:
1. Explicit StateGraph with typed reducers and deterministic edge routing.
2. Deterministic conditional edges (gate-driven routing with bounded retry).
3. Durable Checkpointing (SQLite WAL, JSON, or In-Memory) at node boundaries
   allowing long-running tasks to resume from the last verified node after interruptions.
4. Recursion limits and tool-loop stagnation guards to prevent infinite loops.
5. Mandatory terminal worktree harvest verification so worktree commits are never stranded.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# Canonical graph boundary sentinels
START: str = "__start__"
END: str = "__end__"


class StateGraphError(Exception):
    """Base error for state graph execution."""


class RecursionLimitExceeded(StateGraphError):
    """Raised when execution step count exceeds recursion limit."""


class StagnationDetectedError(StateGraphError):
    """Raised when execution repeats identical states without forward progress."""


class WorktreeStrandedError(StateGraphError):
    """Raised when verified worktree commits or changes are not harvested before completion."""


class InvalidGraphError(StateGraphError):
    """Raised when the graph configuration is topologically invalid."""


class NodeExecutionError(StateGraphError):
    """Raised when a graph node fails during execution."""


@dataclass
class Checkpoint:
    """Snapshot of graph state at a node boundary."""
    task_id: str
    step: int
    node: str
    state: Dict[str, Any]
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Checkpoint:
        return cls(
            task_id=str(data["task_id"]),
            step=int(data["step"]),
            node=str(data["node"]),
            state=dict(data.get("state") or {}),
            created_at=float(data.get("created_at") or time.time()),
            metadata=dict(data.get("metadata") or {}),
        )


class BaseCheckpointer(ABC):
    """Abstract durable checkpoint storage interface."""

    @abstractmethod
    def put(self, checkpoint: Checkpoint) -> None:
        """Persist a checkpoint."""

    @abstractmethod
    def get(self, task_id: str) -> Optional[Checkpoint]:
        """Retrieve the latest checkpoint for a task."""

    @abstractmethod
    def list(self, task_id: str) -> List[Checkpoint]:
        """List all historical checkpoints for a task in chronological order."""


class MemoryCheckpointer(BaseCheckpointer):
    """Ephemeral in-memory checkpointer for testing and lightweight operations."""

    def __init__(self) -> None:
        self._storage: Dict[str, List[Checkpoint]] = {}

    def put(self, checkpoint: Checkpoint) -> None:
        ck = copy.deepcopy(checkpoint)
        self._storage.setdefault(ck.task_id, []).append(ck)

    def get(self, task_id: str) -> Optional[Checkpoint]:
        history = self._storage.get(task_id)
        if not history:
            return None
        return copy.deepcopy(history[-1])

    def list(self, task_id: str) -> List[Checkpoint]:
        return copy.deepcopy(self._storage.get(task_id, []))


class SqliteCheckpointer(BaseCheckpointer):
    """Durable WAL-mode SQLite checkpointer ensuring crash resilience."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS state_checkpoints (
                    task_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    node TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (task_id, step)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chk_task_step ON state_checkpoints(task_id, step DESC)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def put(self, checkpoint: Checkpoint) -> None:
        state_str = json.dumps(checkpoint.state, ensure_ascii=False)
        meta_str = json.dumps(checkpoint.metadata, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO state_checkpoints
                (task_id, step, node, state_json, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (checkpoint.task_id, checkpoint.step, checkpoint.node,
                  state_str, checkpoint.created_at, meta_str))
            conn.commit()

    def get(self, task_id: str) -> Optional[Checkpoint]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT task_id, step, node, state_json, created_at, metadata_json
                FROM state_checkpoints
                WHERE task_id = ?
                ORDER BY step DESC LIMIT 1
            """, (task_id,)).fetchone()
            if not row:
                return None
            return Checkpoint(
                task_id=row["task_id"],
                step=row["step"],
                node=row["node"],
                state=json.loads(row["state_json"]),
                created_at=row["created_at"],
                metadata=json.loads(row["metadata_json"] or "{}"),
            )

    def list(self, task_id: str) -> List[Checkpoint]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT task_id, step, node, state_json, created_at, metadata_json
                FROM state_checkpoints
                WHERE task_id = ?
                ORDER BY step ASC
            """, (task_id,)).fetchall()
            return [
                Checkpoint(
                    task_id=r["task_id"],
                    step=r["step"],
                    node=r["node"],
                    state=json.loads(r["state_json"]),
                    created_at=r["created_at"],
                    metadata=json.loads(r["metadata_json"] or "{}"),
                ) for r in rows
            ]


class JsonCheckpointer(BaseCheckpointer):
    """File-system durable checkpointer writing immutable step JSON files."""

    def __init__(self, root_dir: Union[str, Path]) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_id: str) -> Path:
        safe_id = re.sub(r"[^\w\-.]", "_", task_id)
        td = (self.root_dir / safe_id).resolve()
        if not str(td).startswith(str(self.root_dir)):
            td = self.root_dir / hashlib.sha256(task_id.encode()).hexdigest()
        td.mkdir(parents=True, exist_ok=True)
        return td

    def put(self, checkpoint: Checkpoint) -> None:
        td = self._task_dir(checkpoint.task_id)
        filename = f"step_{checkpoint.step:06d}_{checkpoint.node}.json"
        tmp_file = td / f".tmp_{filename}"
        target_file = td / filename
        data = checkpoint.to_dict()
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, target_file)

    def get(self, task_id: str) -> Optional[Checkpoint]:
        history = self.list(task_id)
        return history[-1] if history else None

    def list(self, task_id: str) -> List[Checkpoint]:
        td = self._task_dir(task_id)
        if not td.is_dir():
            return []
        items = []
        for p in sorted(td.glob("step_*_*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    items.append(Checkpoint.from_dict(json.load(f)))
            except Exception as e:
                logger.warning("Failed to load checkpoint file %s: %s", p, e)
        items.sort(key=lambda c: c.step)
        return items


def _state_hash(state: Dict[str, Any]) -> str:
    """Compute deterministic content hash of state to detect stagnation."""
    try:
        serialized = json.dumps(state, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        serialized = str(sorted(state.items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def check_worktree_harvest(
    worktree: Union[str, Path],
    base: Optional[str] = None,
) -> bool:
    """Verify that a git worktree has no uncommitted dirty changes or stranded unmerged commits."""
    p = Path(worktree).resolve()
    if not p.is_dir() or not (p / ".git").exists():
        return True
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(p),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        dirty = bool(status.stdout.strip())
        candidates = [base] if base else ["main", "master"]
        unharvested_commits: List[str] = []
        for base_candidate in candidates:
            if not base_candidate:
                continue
            ahead_check = subprocess.run(
                ["git", "rev-list", f"{base_candidate}..HEAD"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if ahead_check.returncode == 0:
                unharvested_commits = [c for c in ahead_check.stdout.split() if c.strip()]
                break
    except Exception as exc:
        logger.warning("Worktree harvest check inspect failed: %s", exc)
        return True

    if dirty or unharvested_commits:
        raise WorktreeStrandedError(
            f"Worktree at '{p}' has {len(unharvested_commits)} unmerged commits "
            f"or dirty changes ({dirty}) that have not been harvested!"
        )
    return True


class StateGraph:
    """Declarative directed graph managing state transitions, conditional edges, and reducers."""

    def __init__(self, state_schema: Optional[type] = None) -> None:
        self.state_schema = state_schema
        self.nodes: Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}
        self.edges: Dict[str, str] = {}
        self.conditional_edges: Dict[str, Tuple[Callable[[Dict[str, Any]], str], Optional[Dict[str, str]]]] = {}
        self.entry_point: Optional[str] = None
        self.reducers: Dict[str, Callable[[Any, Any], Any]] = {}

    def add_node(self, name: str, fn: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> StateGraph:
        if name in (START, END):
            raise InvalidGraphError(f"Node name cannot be reserved sentinel '{name}'")
        if not callable(fn):
            raise InvalidGraphError(f"Node '{name}' must have a callable function")
        if name in self.nodes:
            raise InvalidGraphError(f"Node '{name}' already exists in graph")
        self.nodes[name] = fn
        return self

    def set_entry_point(self, name: str) -> StateGraph:
        if name not in self.nodes:
            raise InvalidGraphError(f"Entry point '{name}' is not a registered node")
        self.entry_point = name
        return self

    def set_finish_point(self, name: str) -> StateGraph:
        return self.add_edge(name, END)

    def add_edge(self, start_node: str, end_node: str) -> StateGraph:
        if start_node == END:
            raise InvalidGraphError("Cannot route from END node")
        if start_node == START:
            return self.set_entry_point(end_node)
        if start_node not in self.nodes:
            raise InvalidGraphError(f"Edge source '{start_node}' is not registered")
        if end_node != END and end_node not in self.nodes:
            raise InvalidGraphError(f"Edge target '{end_node}' is not registered")
        if start_node in self.edges or start_node in self.conditional_edges:
            raise InvalidGraphError(f"Edge already defined for source '{start_node}'")
        self.edges[start_node] = end_node
        return self

    def add_conditional_edges(
        self,
        source: str,
        path: Callable[[Dict[str, Any]], str],
        path_map: Optional[Dict[str, str]] = None,
    ) -> StateGraph:
        if source == END:
            raise InvalidGraphError("Cannot route from END node")
        if source not in self.nodes:
            raise InvalidGraphError(f"Conditional edge source '{source}' is not registered")
        if not callable(path):
            raise InvalidGraphError("Conditional edge path router must be callable")
        if source in self.edges or source in self.conditional_edges:
            raise InvalidGraphError(f"Edge already defined for source '{source}'")
        if path_map is not None:
            for val, target in path_map.items():
                if target != END and target not in self.nodes:
                    raise InvalidGraphError(f"Conditional edge target '{target}' for value '{val}' is not registered")
        self.conditional_edges[source] = (path, path_map)
        return self

    def register_reducer(self, key: str, reducer: Callable[[Any, Any], Any]) -> StateGraph:
        if not callable(reducer):
            raise InvalidGraphError(f"Reducer for key '{key}' must be callable")
        self.reducers[key] = reducer
        return self

    def compile(
        self,
        checkpointer: Optional[BaseCheckpointer] = None,
        recursion_limit: int = 25,
        stagnation_limit: int = 5,
        worktree_harvest_check: bool = False,
        harvest_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> CompiledGraph:
        if not self.entry_point:
            raise InvalidGraphError("Graph entry point is not set")
        if recursion_limit <= 0:
            raise InvalidGraphError("recursion_limit must be positive")
        if stagnation_limit <= 0:
            raise InvalidGraphError("stagnation_limit must be positive")

        return CompiledGraph(
            nodes=dict(self.nodes),
            edges=dict(self.edges),
            conditional_edges=dict(self.conditional_edges),
            entry_point=self.entry_point,
            reducers=dict(self.reducers),
            checkpointer=checkpointer,
            recursion_limit=recursion_limit,
            stagnation_limit=stagnation_limit,
            worktree_harvest_check=worktree_harvest_check,
            harvest_callback=harvest_callback,
        )


class CompiledGraph:
    """Compiled runnable execution engine for StateGraph."""

    def __init__(
        self,
        nodes: Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]],
        edges: Dict[str, str],
        conditional_edges: Dict[str, Tuple[Callable[[Dict[str, Any]], str], Optional[Dict[str, str]]]],
        entry_point: str,
        reducers: Dict[str, Callable[[Any, Any], Any]],
        checkpointer: Optional[BaseCheckpointer] = None,
        recursion_limit: int = 25,
        stagnation_limit: int = 5,
        worktree_harvest_check: bool = False,
        harvest_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.conditional_edges = conditional_edges
        self.entry_point = entry_point
        self.reducers = reducers
        self.checkpointer = checkpointer
        self.recursion_limit = recursion_limit
        self.stagnation_limit = stagnation_limit
        self.worktree_harvest_check = worktree_harvest_check
        self.harvest_callback = harvest_callback

    def _apply_updates(self, current_state: Dict[str, Any], updates: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply node updates to current state with registered reducers."""
        new_state = copy.deepcopy(current_state)
        if not updates or not isinstance(updates, dict):
            return new_state
        for key, val in updates.items():
            if key in self.reducers:
                old_val = new_state.get(key)
                new_state[key] = self.reducers[key](old_val, val)
            else:
                new_state[key] = copy.deepcopy(val)
        return new_state

    def _check_worktree_harvest(self, state: Dict[str, Any]) -> None:
        """Mandatory terminal gate: ensure verified worktree commits are never silently stranded."""
        wt_path = state.get("worktree") or state.get("worktree_path")
        if not wt_path:
            return

        p = Path(wt_path)
        if not p.is_dir() or not (p / ".git").exists():
            return

        try:
            # Check for uncommitted dirty changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(p),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            dirty = bool(status.stdout.strip())

            # Check for unmerged commits ahead of base
            candidates = [state.get("worktree_base")] if state.get("worktree_base") else ["main", "master"]
            unharvested_commits = []
            for base_candidate in candidates:
                if not base_candidate:
                    continue
                ahead_check = subprocess.run(
                    ["git", "rev-list", f"{base_candidate}..HEAD"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if ahead_check.returncode == 0:
                    unharvested_commits = [c for c in ahead_check.stdout.split() if c.strip()]
                    break
        except Exception as exc:
            logger.warning("Worktree harvest check inspect failed: %s", exc)
            return

        is_stranded = dirty or bool(unharvested_commits)
        if not is_stranded:
            state["worktree_harvested"] = True
            return

        # Attempt recovery via harvest_callback if provided
        if callable(self.harvest_callback):
            try:
                harvest_res = self.harvest_callback(state)
                if harvest_res is True or (isinstance(harvest_res, dict) and harvest_res.get("ok")):
                    state["worktree_harvested"] = True
                    return
            except Exception as e:
                logger.error("Harvest callback failed: %s", e)

        state["worktree_harvested"] = False
        state["worktree_unharvested_commits"] = unharvested_commits
        state["worktree_dirty"] = dirty
        raise WorktreeStrandedError(
            f"Worktree at '{wt_path}' has {len(unharvested_commits)} unmerged commits "
            f"or dirty changes ({dirty}) that have not been harvested!"
        )

    def stream(
        self,
        initial_state: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Stream execution node by node, yielding (node_name, state)."""
        cfg = config or {}
        task_id = str(cfg.get("task_id") or hashlib.sha256(str(time.time()).encode()).hexdigest()[:32])
        rec_limit = int(cfg.get("recursion_limit") or self.recursion_limit)
        stag_limit = int(cfg.get("stagnation_limit") or self.stagnation_limit)
        checkpointer = cfg.get("checkpointer") or self.checkpointer

        current_node = self.entry_point
        state = copy.deepcopy(initial_state)
        step = 0

        # Resume from checkpoint if available
        if checkpointer:
            last_ckpt = checkpointer.get(task_id)
            if last_ckpt:
                merged = copy.deepcopy(initial_state)
                merged.update(copy.deepcopy(last_ckpt.state))
                state = merged
                step = last_ckpt.step
                state["_resumed_from_node"] = last_ckpt.node
                state["_resumed_from_step"] = last_ckpt.step
                logger.info("Resuming task %s from checkpoint step %d at node %s", task_id, step, last_ckpt.node)
                # Next node is routed from the checkpoint's node
                if last_ckpt.node in self.conditional_edges:
                    router, path_map = self.conditional_edges[last_ckpt.node]
                    route_val = router(state)
                    if path_map is not None:
                        if route_val not in path_map:
                            raise InvalidGraphError(
                                f"Conditional edge router from '{last_ckpt.node}' returned '{route_val}', "
                                f"which is not in path_map: {list(path_map.keys())}"
                            )
                        current_node = path_map[route_val]
                    else:
                        current_node = route_val
                elif last_ckpt.node in self.edges:
                    current_node = self.edges[last_ckpt.node]
                else:
                    current_node = END

        stagnation_counter = 0
        last_state_hash = _state_hash(state)

        while current_node != END:
            step += 1
            if step > rec_limit:
                raise RecursionLimitExceeded(
                    f"StateGraph recursion limit ({rec_limit}) reached at node '{current_node}'"
                )

            node_fn = self.nodes.get(current_node)
            if not node_fn:
                raise InvalidGraphError(f"Node '{current_node}' not found during execution")

            logger.debug("Executing node '%s' (step %d)", current_node, step)
            try:
                updates = node_fn(state)
            except Exception as exc:
                raise NodeExecutionError(f"Node '{current_node}' failed: {exc}") from exc

            state = self._apply_updates(state, updates)

            # Stagnation Guard: check content delta
            curr_hash = _state_hash(state)
            if curr_hash == last_state_hash:
                stagnation_counter += 1
                if stagnation_counter >= stag_limit:
                    raise StagnationDetectedError(
                        f"StateGraph stagnation detected: node '{current_node}' repeated "
                        f"{stagnation_counter} times with zero state progress."
                    )
            else:
                stagnation_counter = 0
                last_state_hash = curr_hash

            # Persist checkpoint at node boundary
            if checkpointer:
                ckpt = Checkpoint(
                    task_id=task_id,
                    step=step,
                    node=current_node,
                    state=copy.deepcopy(state),
                    created_at=time.time(),
                )
                checkpointer.put(ckpt)

            yield (current_node, copy.deepcopy(state))

            # Determine next edge
            if current_node in self.conditional_edges:
                router, path_map = self.conditional_edges[current_node]
                route_val = router(state)
                if path_map is not None:
                    if route_val not in path_map:
                        raise InvalidGraphError(
                            f"Conditional edge router from '{current_node}' returned '{route_val}', "
                            f"which is not in path_map: {list(path_map.keys())}"
                        )
                    next_node = path_map[route_val]
                else:
                    next_node = route_val
            elif current_node in self.edges:
                next_node = self.edges[current_node]
            else:
                next_node = END

            current_node = next_node

        # Mandatory terminal check before finishing
        if self.worktree_harvest_check or cfg.get("worktree_harvest_check"):
            self._check_worktree_harvest(state)

        yield (END, copy.deepcopy(state))

    def invoke(
        self,
        initial_state: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute the graph to completion, returning final state."""
        final_state = copy.deepcopy(initial_state)
        for _, state in self.stream(initial_state, config=config):
            final_state = state
        return final_state
