# Tool fanout scheduler implementation

Date: 2026-10-26.

## Receipt

- Conclusion: added a bounded scheduler with explicit standard-library and optional native FlowCoro backends. No production service was restarted or reconfigured.
- Code: `agentcore/scheduler.py`; tests: `tests/test_scheduler.py`.
- Local verification with the real native build on `PYTHONPATH`: scheduler, repository neutrality, package neutrality and import-boundary tests: **48 passed, 0 skipped** (2.02s). Standard-library and native scheduler scenarios both passed. Without that optional runtime path, the baseline interpreter reports **40 passed, 8 skipped**; these skips are explicitly reported rather than silently substituting threads.
- User acceptance: pending.
- No commit or push performed by this subtask.

## Upstream mechanisms reused

Read upstream `caixuf/flowcoro` main-branch sources; the isolated real native build was pinned to commit `aaac885e673e01b6f21037a47930ef744668d26b` (binding version 4.0.0):

- https://github.com/caixuf/flowcoro/blob/main/docs/AGENT_SCHED.md
- https://github.com/caixuf/flowcoro/blob/main/docs/PYTHON_BINDING.md
- https://github.com/caixuf/flowcoro/blob/main/examples/python/flowcoro_agent.py

Reuse: native `CoroutineThreadPool.submit()` and one native `CancellationTokenSource` per submitted tool. Completion is bridged to standard `Future` condition events, so the same waiting semantics work for both backends without checking `done()` in a busy loop. We intentionally do not copy the example's permanently growing `_handles` list. Only unfinished handles are retained in the scheduler's active set, and completion removes them.

We do not claim a speedup over standard threads. This is same-process tool fanout; process lifetime, durable attempts, job fencing, persistence and external message delivery belong to higher layers.

## Public surface

```python
from agentcore.scheduler import AgentScheduler  # Scheduler is an alias

with AgentScheduler(backend='stdlib', workers=4, capacity=16) as scheduler:
    handle = scheduler.submit('lookup', lookup, query, admission_timeout=1)
    index = scheduler.wait_first([handle], timeout=5)  # index or None
    results = scheduler.gather([handle], timeout=30)  # input order
```

- `backend='flowcoro'` imports `flowcoro_py` dynamically and uses the real native pool. Missing binding raises `ImportError`; no silent fallback.
- `capacity` bounds all unfinished work, including running and queued tools. Admission blocks on a condition until capacity is available; `admission_timeout=0` is immediate rejection. Closing wakes blocked submitters with `RuntimeError`.
- `submit_tool` aliases `submit`; `Scheduler` aliases `AgentScheduler`.
- Handles expose `name`, `done()`, `result(timeout)`, `exception(timeout)`, `cancel()`, `cancellation_requested()` and `cancelled()`.
- A supported `cancel_token` or `cancel_event` keyword is injected only when not already supplied. Supplied positional cancellation arguments are preserved.
- `gather` waits for all handles before raising the first exception in input order. Timed-out tools continue running. Original Python exception objects are preserved.
- `wait_first` returns the earliest recorded completion among the supplied completed handles; empty input or timeout returns `None`. Cross-scheduler handles are rejected.
- `pending_count` reports active unfinished handles. Historical completed work is not retained.

## Cancellation and shutdown semantics

`cancel(handle)` requests cooperative cancellation and returns whether the handle was still unfinished. `cancelled()` becomes true only when the tool actually ends with a cancellation exception. A tool ignoring the token can still finish successfully; a cancellation request is not represented as a successful thread kill.

`close(cancel_pending=False)` prevents new submission and drains all running/queued work; it is idempotent. `cancel_pending=True` first requests cancellation on active handles, then still drains them. An uncooperative or permanently blocked tool can delay close indefinitely. Calling close from a tool belonging to the same scheduler is rejected to prevent self-join deadlock. Blocking nested submission into an already full scheduler is rejected rather than deadlocking itself.

This API cannot undo external side effects and is not a subprocess kill mechanism.

## Reproduction

```bash
cd <technical-repository>
/usr/bin/python3 -m pytest tests/test_scheduler.py tests/test_repo_neutrality.py tests/test_business_neutrality.py tests/test_import_boundary.py -q
```

The scheduler file itself has **18 passing scenarios across both backends** when the native build is available. Gates add 30 passing assertions. Tested risks: bounded worker concurrency, full queue rejection and release, concurrent close/admission, wait timeouts, exception identity, cooperative versus ignored cancellation, queued cancellation on close, cross-pool rejection, self-close rejection, positional argument preservation and completed-handle garbage collection.

Native reproduction used the real optional binding built per upstream `PYTHON_BINDING.md`, not a fabricated module. Exact command:

```bash
PYTHONPATH=/tmp/flowcoro-verify-aHSWNK/build/python /usr/bin/python3 -m pytest tests/test_scheduler.py tests/test_repo_neutrality.py tests/test_business_neutrality.py tests/test_import_boundary.py -q
```

The build resides in a temporary verification directory, not an installed production dependency. Other machines must build the binding for their own interpreter and expose that directory before selecting `backend=flowcoro`. Packaging the native module, building wheels and changing FlowCoro source are outside this patch.
