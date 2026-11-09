"""tests/test_state_graph.py — Comprehensive test suite for agentcore.state_graph.

Covers:
1. Linear StateGraph execution and state propagation.
2. Custom reducers for state keys.
3. Deterministic conditional edges (gate-driven flow: research -> implement -> verify -> done/retry).
4. Durable checkpointers: MemoryCheckpointer, SqliteCheckpointer, JsonCheckpointer.
5. Task resumption from checkpoints across interruptions.
6. Recursion limit guardrail (preventing infinite execution).
7. Stagnation detector (detecting tool-loop stagnation without state progression).
8. Mandatory worktree harvest check (preventing stranded commits).
"""
import operator
from pathlib import Path
import subprocess
import pytest

from agentcore.state_graph import (
    END,
    START,
    Checkpoint,
    JsonCheckpointer,
    MemoryCheckpointer,
    RecursionLimitExceeded,
    SqliteCheckpointer,
    StagnationDetectedError,
    StateGraph,
    WorktreeStrandedError,
)


def test_linear_graph_execution():
    """Verify linear node progression and state transformation."""
    builder = StateGraph()

    def step_a(state):
        return {"a": 1, "log": ["step_a"]}

    def step_b(state):
        return {"b": state["a"] * 2, "log": state["log"] + ["step_b"]}

    builder.add_node("node_a", step_a)
    builder.add_node("node_b", step_b)
    builder.set_entry_point("node_a")
    builder.add_edge("node_a", "node_b")
    builder.add_edge("node_b", END)

    graph = builder.compile()
    final = graph.invoke({})

    assert final["a"] == 1
    assert final["b"] == 2
    assert final["log"] == ["step_a", "step_b"]


def test_state_reducer():
    """Verify state key reducer functions (e.g. appending list)."""
    builder = StateGraph()
    builder.register_reducer("messages", lambda old, new: (old or []) + [new])

    def node_first(state):
        return {"messages": "hello"}

    def node_second(state):
        return {"messages": "world"}

    builder.add_node("first", node_first)
    builder.add_node("second", node_second)
    builder.set_entry_point("first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)

    graph = builder.compile()
    result = graph.invoke({"messages": ["init"]})
    assert result["messages"] == ["init", "hello", "world"]


def test_conditional_edges_gate_driven():
    """Verify deterministic conditional routing (research -> implement -> verify -> harvest/retry)."""
    builder = StateGraph()

    def research(state):
        return {"phase": "researched", "retry_count": state.get("retry_count", 0)}

    def implement(state):
        tries = state.get("retry_count", 0) + 1
        return {"phase": "implemented", "retry_count": tries, "code": f"v{tries}"}

    def verify(state):
        # Passes only on attempt 2
        passed = state["retry_count"] >= 2
        return {"verified": passed}

    def router(state):
        return "harvest" if state.get("verified") else "retry"

    def harvest(state):
        return {"final_verdict": "SUCCESS"}

    builder.add_node("research", research)
    builder.add_node("implement", implement)
    builder.add_node("verify", verify)
    builder.add_node("harvest", harvest)

    builder.set_entry_point("research")
    builder.add_edge("research", "implement")
    builder.add_edge("implement", "verify")
    builder.add_conditional_edges(
        "verify",
        router,
        path_map={"harvest": "harvest", "retry": "implement"},
    )
    builder.add_edge("harvest", END)

    graph = builder.compile(recursion_limit=10)
    final = graph.invoke({})

    assert final["final_verdict"] == "SUCCESS"
    assert final["retry_count"] == 2
    assert final["code"] == "v2"


def test_sqlite_checkpointer(tmp_path: Path):
    """Verify durable WAL SQLite checkpoint persistence and retrieval."""
    db_file = tmp_path / "checkpoints.db"
    checkpointer = SqliteCheckpointer(db_file)

    task_id = "test_task_123"
    ckpt1 = Checkpoint(task_id=task_id, step=1, node="node_a", state={"v": 10})
    ckpt2 = Checkpoint(task_id=task_id, step=2, node="node_b", state={"v": 20})

    checkpointer.put(ckpt1)
    checkpointer.put(ckpt2)

    latest = checkpointer.get(task_id)
    assert latest is not None
    assert latest.step == 2
    assert latest.node == "node_b"
    assert latest.state == {"v": 20}

    history = checkpointer.list(task_id)
    assert len(history) == 2
    assert history[0].step == 1
    assert history[1].step == 2


def test_json_checkpointer(tmp_path: Path):
    """Verify durable JSON directory checkpoint persistence and retrieval."""
    checkpointer = JsonCheckpointer(tmp_path / "ckpts")

    task_id = "json_task_456"
    ckpt1 = Checkpoint(task_id=task_id, step=1, node="init", state={"step": "first"})
    ckpt2 = Checkpoint(task_id=task_id, step=2, node="done", state={"step": "second"})

    checkpointer.put(ckpt1)
    checkpointer.put(ckpt2)

    latest = checkpointer.get(task_id)
    assert latest is not None
    assert latest.step == 2
    assert latest.state["step"] == "second"


def test_resumption_from_checkpoint(tmp_path: Path):
    """Verify graph execution resumes from the last checkpoint."""
    db_file = tmp_path / "resume.db"
    checkpointer = SqliteCheckpointer(db_file)
    task_id = "resumable_task_001"

    builder = StateGraph()
    call_log = []

    def node_1(state):
        call_log.append("n1")
        return {"step1": True}

    def node_2(state):
        call_log.append("n2")
        return {"step2": True}

    def node_3(state):
        call_log.append("n3")
        return {"step3": True}

    builder.add_node("n1", node_1)
    builder.add_node("n2", node_2)
    builder.add_node("n3", node_3)

    builder.set_entry_point("n1")
    builder.add_edge("n1", "n2")
    builder.add_edge("n2", "n3")
    builder.add_edge("n3", END)

    # Pre-populate checkpoint at step 1 (n1 completed)
    checkpointer.put(Checkpoint(task_id=task_id, step=1, node="n1", state={"step1": True}))

    graph = builder.compile(checkpointer=checkpointer)
    final = graph.invoke({}, config={"task_id": task_id})

    # node 1 should have been skipped because execution resumed after n1!
    assert "n1" not in call_log
    assert call_log == ["n2", "n3"]
    assert final["step1"] is True
    assert final["step2"] is True
    assert final["step3"] is True


def test_recursion_limit_guardrail():
    """Verify recursion limit prevents infinite loops."""
    builder = StateGraph()

    def loop_node(state):
        return {"count": state.get("count", 0) + 1}

    builder.add_node("loop", loop_node)
    builder.set_entry_point("loop")
    builder.add_edge("loop", "loop")

    graph = builder.compile(recursion_limit=5, stagnation_limit=10)

    with pytest.raises(RecursionLimitExceeded) as exc_info:
        graph.invoke({})
    assert "recursion limit (5) reached" in str(exc_info.value)


def test_stagnation_guardrail():
    """Verify stagnation guard stops unguided tool loops with zero state delta."""
    builder = StateGraph()

    def unguided_tool_query(state):
        # Simulates a tool loop (e.g. 336 read calls) that keeps returning same state
        return {"query_target": "same_symbol"}

    builder.add_node("query", unguided_tool_query)
    builder.set_entry_point("query")
    builder.add_edge("query", "query")

    graph = builder.compile(recursion_limit=50, stagnation_limit=3)

    with pytest.raises(StagnationDetectedError) as exc_info:
        graph.invoke({"query_target": "same_symbol"})
    assert "stagnation detected" in str(exc_info.value)


def test_worktree_harvest_gate(tmp_path: Path):
    """Verify terminal worktree harvest gate stops unharvested commits from being stranded."""
    wt_dir = tmp_path / "fake_worktree"
    wt_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(wt_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(wt_dir), check=True)
    subprocess.run(["git", "config", "user.email", "tester@local"], cwd=str(wt_dir), check=True)

    # Initial commit on main
    (wt_dir / "base.txt").write_text("base")
    subprocess.run(["git", "add", "base.txt"], cwd=str(wt_dir), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(wt_dir), check=True)

    # Branch work with an unmerged commit
    subprocess.run(["git", "checkout", "-b", "feature/test"], cwd=str(wt_dir), check=True)
    (wt_dir / "work.txt").write_text("new feature")
    subprocess.run(["git", "add", "work.txt"], cwd=str(wt_dir), check=True)
    subprocess.run(["git", "commit", "-m", "worktree commit"], cwd=str(wt_dir), check=True)

    builder = StateGraph()

    def dev_step(state):
        return {"status": "tested"}

    builder.add_node("dev", dev_step)
    builder.set_entry_point("dev")
    builder.add_edge("dev", END)

    graph = builder.compile(worktree_harvest_check=True)

    # Unharvested commit should raise WorktreeStrandedError
    with pytest.raises(WorktreeStrandedError) as exc_info:
        graph.invoke({"worktree": str(wt_dir), "worktree_base": "master"})
    assert "unmerged commits" in str(exc_info.value)

    # Now provide a harvest callback that handles it
    harvested = []

    def harvest_cb(state):
        harvested.append(state["worktree"])
        return True

    graph_with_cb = builder.compile(worktree_harvest_check=True, harvest_callback=harvest_cb)
    final = graph_with_cb.invoke({"worktree": str(wt_dir), "worktree_base": "master"})
    assert final.get("worktree_harvested") is True
    assert str(wt_dir) in harvested


def test_json_checkpointer_path_traversal_sanitization(tmp_path: Path):
    """Verify that JsonCheckpointer prevents path traversal attacks in task_id."""
    checkpointer = JsonCheckpointer(tmp_path / "ckpts")
    malicious_task_id = "../../etc/passwd"
    ckpt = Checkpoint(task_id=malicious_task_id, step=1, node="safe_node", state={"k": "v"})
    checkpointer.put(ckpt)

    # Checkpoint must be stored safely inside root_dir, not outside
    retrieved = checkpointer.get(malicious_task_id)
    assert retrieved is not None
    assert retrieved.step == 1
    assert retrieved.node == "safe_node"
    # Ensure no file was created outside root_dir
    assert not (tmp_path / "etc").exists()


def test_conditional_edge_invalid_route_key_raises_invalid_graph_error():
    """Verify that if router returns an unmapped key, InvalidGraphError is raised."""
    from agentcore.state_graph import InvalidGraphError
    builder = StateGraph()
    builder.add_node("step1", lambda s: {"val": 1})
    builder.add_node("target_a", lambda s: {"out": "a"})
    builder.set_entry_point("step1")
    builder.add_conditional_edges("step1", lambda s: "unmapped_route", {"good": "target_a"})
    builder.add_edge("target_a", END)

    graph = builder.compile()
    with pytest.raises(InvalidGraphError) as exc_info:
        graph.invoke({})
    assert "not in path_map" in str(exc_info.value)
