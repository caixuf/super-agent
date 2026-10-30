# T5 slice B: durable-dispatch completion backfill

- Repo: `~/code/agentcore`, branch `master`, base `322686a`
- Scope: persist the submitting turn's route and route terminal receipts back to that
  conversation through the public Hermes injection API
- Not done: no commit, no restart, no production profile change, no real Feishu send

## What was missing

`pool.outbox` already recorded every terminal job and `jobs.origin` already held
`profile` + `session`, but nothing consumed the receipts. The plugin also never
persisted the route, so even a future consumer could not address a chat without
guessing the active session. `agentcore/completion_delivery.py` existed but had no
transport and no runtime.

## Change

| File | Change |
| :--- | :--- |
| `agentcore/pool.py` | `claim_receipt(lease, skip=())`; skipped receipts stay pending and are skipped in the ledger, not acknowledged |
| `agentcore/completion_delivery.py` | `deliver_once` / `deliver_pending` forward `skip` |
| `agentcore/completion_backfill.py` (new) | Route policy: deliverable = stored `route` + matching profile; `drain`, `parked_jobs`, `inspect`, dry-run CLI |
| `plugins/durable_dispatch/__init__.py` | Persist `route/channel/chat/thread` from the **bound submitting turn** (only when `HERMES_SESSION_ID` agrees with the dispatcher identity), `InjectionTransport`, opt-in poller, `on_unload` stop, canonical four-module engine fallback |
| `plugins/durable_dispatch/plugin.yaml` | 0.2.0 description |
| `plugins/durable_dispatch/README.md` | Delivery contract, config, limits |
| `patches/verify_durable_dispatch_backfill.py` (new) | Host capability harness: `inject_message`, consent gate, bound session vars |
| `tests/test_durable_dispatch_backfill.py` (new) | 14 cases |

`patches/patch_durable_dispatch_visibility.py` needed no change: delivery uses the
already-public injection surface. The production Hermes tree was not modified.

## Semantics

- Address and profile are read only from `jobs.origin`; no active-session inference.
- Unaddressable receipts (legacy origin, foreign profile) stay pending and cannot
  block later completions.
- Ack only after a transport receipt naming the task; `True`/`None`/exception never ack.
- Restart/reconnect re-reads the ledger; delivered receipts are not replayed.
- At-least-once: a crash between send and ack can duplicate; the task id is the
  idempotency key.

## Verification (all on 远端宿主)

```bash
cd ~/code/agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_durable_dispatch_backfill.py -q
# 14 passed

TMPDIR=/dev/shm python3 -m pytest tests/test_durable_dispatch.py \
  tests/test_durable_dispatch_visibility.py tests/test_completion_delivery.py \
  tests/test_pool.py tests/test_worker.py tests/test_durable_dispatch_backfill.py -q
# 78 passed, 1 skipped (flowcoro optional)

TMPDIR=/dev/shm bash verify.sh
# 471 passed, 9 skipped; 9 shell scripts OK; installer dry-run exit 0

python3 patches/verify_durable_dispatch_backfill.py
# [PASS] inject_message + consent gate + bound session route are available
```

The real-host case launches the production Hermes interpreter with an isolated
`HERMES_HOME`, binds the real `gateway.session_context.set_session_vars`, asserts
`_bound_route` reads exactly `route/channel/chat/thread` (and refuses a mismatched
session id), publishes a fake gateway injector with
`manager.set_gateway_message_injector`, and asserts the stored route reaches
`inject_message`, the task id appears in the rendered text, and the ledger row is
acknowledged with a receipt id. No WS connection, no Feishu call.

## Residual gaps

1. **Exact session pinning after `/new` is not available.** The public
   `inject_message` addresses a route key, not a durable session id; Hermes resolves
   the route's current session. The persisted session id is audit metadata only.
2. **`inject_message` returning True means host acceptance, not adapter delivery.**
   The live gateway may still fail the scheduled turn; that failure is logged host-side
   and does not revoke the ledger ack. There is no synchronous delivery receipt in the
   public plugin API.
3. **Delivery re-enters the session as an internal turn.** Hermes exposes no public
   "send without a model turn" primitive; a direct card/message emit requires the
   G2b host patch (`gateway/session_automation.py`, `run_notifications.py`), which
   this slice deliberately did not write or apply.
4. **Activation is off.** `deliver_completions` defaults false and production also
   lacks `plugins.entries.durable-dispatch.allow_gateway_injection: true`. Enabling
   is a deployment action with an isolated-profile check and user-visible review.
5. **Head-of-line scan is bounded** at 500 pending receipts per pass; if more than
   500 permanently unaddressable rows accumulate, later rows wait for an operator.
6. **At-least-once only**; no exactly-once claim across a send/ack crash.
7. **Plugin host isolation (`plugins.isolation: host`) cannot capture a route.** The
   plugin then runs in a separate process where the gateway's bound session
   ContextVars are absent; submissions persist no route and completions park. The
   current host serves plugins in-process. Host mode needs a future public API that
   hands the calling route to plugin code.
8. No real chat window was observed, and no production queue or profile was mutated.

## Rollback

Remove the two new files/tests and `git checkout -- agentcore/pool.py
agentcore/completion_delivery.py plugins/durable_dispatch/ README`; the outbox schema
is unchanged, so no migration rollback is needed.
