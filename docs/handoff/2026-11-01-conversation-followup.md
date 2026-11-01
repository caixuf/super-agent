# Conversation follow-up: observed bare action tails

## Why this follow-up exists

The first Chinese continuation patch was deployed, but later actual replies
ended with these still-unrecognized sentences, followed by no tool action:

- “看这个测试怎么判定。”
- “查这个新失败的根因。”
- “看灌灌那段召回。”

This follow-up repairs that specific omission. It does not claim that a regex
can decide whether every natural-language task is complete.

## Change and safety boundary

The existing host `trailing_continue_intent` classifier now also recognizes
bare 看/查/查看 action tails with a technical diagnostic object. Generic actions
like checking weather, destructive service operations, advice, questions,
quotations, permission waits, negations and result/completion statements do
not gain continuation. Host length, available-tool and two-continuation caps
remain unchanged. English behavior is unchanged.

The patcher supports pristine source, the exact previous released helper and
the new helper. Exact old helpers upgrade; partial/drifted helpers fail closed
without writing. The previous helper is retained as an upgrade fingerprint,
not used as another runtime implementation.

## Verification

Before implementation, the added positive cases produced 5 failures on the old
classifier (including all three observed forms). Tests execute patched source
and the real host finalizer on temporary copies. The deterministic replay
checks early announcement -> non-final interim + nudge -> supplied tool result
-> normal completed final answer, one final stop row and durable flush.

This is a control-flow replay, not a real model or messaging-channel acceptance
test. Stop-gate and output-transform boundaries are mocked as in the existing
test; task correctness and the supplied test result are not inferred by it.

## Still not solved by this patch

- Stale claims about git push/deployment need current-state evidence.
- API disconnections are a separate transport problem.
- True pause/cancel controls are not wired by this change.
- Memory backlog is not consumed or promoted.
- A long or differently worded action tail can still be missed.

Do not reset the host checkout or include unrelated user edits in a release.
Apply to a staged copy, verify, back up exact touched files and activate only
when the authorized instance is idle. Record activation separately below.

## Follow-up checks before activation

- Final Chinese-classifier/real-finalizer + neutrality run: 99 passed in 9.34s
  (a further legacy CLI check/apply test was subsequently added).
- Existing host stall/intent tests against exact final staged helper: 46 passed
  in 0.77s, using system pytest plus host dependencies in an isolated home.
- The installed host venv lacks pytest; no packages were installed to run tests.
- Initial combined run exposed 2 existing fixture assumptions: background patch
  tests expected an unpatched host although the inspected host is now deployed.
  The fixture now validates exact patch shape and reconstructs only known old
  fragments inside copied files, checking round-trip equality. Unknown/partial
  drift still fails; installed host bytes are never changed by these tests.
  Updated background suite: 21 passed in 18.24s.

## Memory diagnostic companion

A new aggregate-only diagnostic (`tools/memory_health.py`) was implemented by
the explicitly requested main model and independently reviewed/run. It never
initializes stores, ingests records, starts a worker or reads memory-file text.
Its 13 tests cover isolation, missing/corrupt inputs, read-only DB/WAL access,
privacy, pending-completed warnings and no worker-liveness inference.

A live read-only invocation found 8 entities / 9 rules / 0 imported rules,
154 completed / 4 discarded / 0 enqueued records, all matching the explicitly
supplied identity. It returned degraded with pending-ingestion and source/index
synchronization warnings. Filesystem mtime comparison is heuristic, not proof
of imported source identity. No backlog was consumed or promoted.

Final pure-release verification (HEAD archive plus only this release's files,
excluding unrelated working-tree edits):

```text
HERMES_SOURCE=<host> TMPDIR=<short-temp> python3 -m pytest
  tests/test_chinese_continue_intent_patch.py
  tests/test_background_verification_patch.py
  tests/test_conversation_commands_real_host.py
  tests/test_task_progress_plugin.py tests/test_repo_neutrality.py
  tests/test_memory_health.py -q
186 passed in 94.74s
```

No real model calls or user-channel messages were made by these tests.
