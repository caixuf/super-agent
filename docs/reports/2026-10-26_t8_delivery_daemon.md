# T8 alert delivery daemon — 2026-10-26

## Status
DONE (new files only; default-off; no commit, no production config, no external network).
The daemon is an opt-in loop over the existing durable outbox; `ops_alerts.py` is unchanged.

## Files (all new, untracked)
- `agentcore/ops_alert_daemon.py`
- `tests/test_ops_alert_daemon.py`
- `docs/reports/2026-10-26_t8_delivery_daemon.md` (this report)

## API
- `DaemonStats(iterations=0, delivered=0, empty=0, failed=0, last_status=None)` (frozen dataclass)
  - per-`run` counters; `last_status` is the final `deliver_once` status string
- `AlertDeliveryDaemon(outbox, sender, *, lease=60.0, idle_interval=5.0,
  backoff_initial=1.0, backoff_max=60.0, sleep=None)`
  - validates: `outbox` and `sender` are required (no invented transport), `lease` /
    `idle_interval` / `backoff_*` must be finite positive numbers (bools, NaN, inf, strings
    rejected), `sleep` must be callable when given
  - holds no globals, starts no thread, reads no env/config, creates no second outbox
  - `run_once() -> dict` — exactly one `ops_alerts.deliver_once(outbox, sender, lease=...)`
  - `run(*, stop=None, max_iterations=None) -> DaemonStats`
    - `stop`: `threading.Event`; omitted -> a fresh event that is never set (long-running mode)
    - `max_iterations`: positive int or `None` (tests / one-shot); validated with `ValueError`
    - one delivery attempt per iteration; `delivered` -> no sleep, next iteration drains the
      next event; `empty` -> wait `idle_interval`; any other status (unavailable,
      delivery_unknown, delivery_rejected, ack_unknown, sent_ack_rejected, or unexpected)
      -> exponential backoff `min(backoff_max, backoff_initial * 2**(n-1))`, exponent capped
      at 62 so pathological failure streaks cannot build a gigantic integer
    - `stop` checked before and after every wait; default wait is `stop.wait(delay)`, so a
      host shutdown interrupts a sleep immediately. An injected `sleep` is only for tests;
      stop is still honored before and after each call.
    - consecutive-failure counter resets on `delivered`/`empty`
- Ack policy is unchanged and delegated: `deliver_once` acknowledges only on
  `DeliveryReceipt(accepted=True, event_id=<this event>, receipt_id=<nonempty>)` (or the
  equivalent dict). Sender exceptions become `delivery_unknown`; the event stays pending.

## Lease interaction (documented, intentional)
A failed attempt leaves the row pending but leased. Until `lease` elapses `claim` returns
`None`, so the loop reports `empty` and backs off/idles. The outbox owns expiry and attempt
accounting; the daemon never clears or rewrites a lease, so a failed event cannot be delivered
twice inside its lease window. No release API was added to `AlertOutbox`.

## Verification
```
ssh 远端宿主
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_ops_alert_daemon.py -q
# 32 passed in 0.35s   (three consecutive runs: 0.35s / 0.30s / 0.44s)
TMPDIR=/dev/shm python3 -m pytest tests/test_ops_alert_outbox.py tests/test_ops_alert_senders.py tests/test_capability_guard.py -q
# 89 passed in 3.59s
TMPDIR=/dev/shm python3 -m pytest tests/test_import_boundary.py tests/test_business_neutrality.py tests/test_repo_neutrality.py -q
# 30 passed in 0.98s
python3 -m py_compile agentcore/ops_alert_daemon.py tests/test_ops_alert_daemon.py
# COMPILE_OK
```

Coverage (32 cases): delivers a pending event once and acks with the receipt stored;
idle polling records exactly `idle_interval` per empty iteration (no busy spin, zero sender
calls); retryable failure stays pending (`delivered`/`receipt_id` NULL, `attempts` grows) and
the next iteration retries after the lease expires; a failed attempt is not redelivered while
the lease holds (`empty`, one sender call); backoff grows 1,2,4 then caps at 5; two pending
events drain one per iteration with no sleep between deliveries and one idle wait at the end;
seven unconfirmed/rejected sender responses never ack; pre-set stop exits with zero iterations
and no claim; stop set during an injected idle wait and during a backoff wait exits gracefully;
the default `stop.wait` path is interrupted promptly from another thread while a 30s idle wait
is in progress; `run_once` reports `delivered` then `empty`; iteration-bound and
timing/constructor validation. No network, no shared database; only the default-wait case
spends 0.1s of real time in a thread.

## Remaining (deliberate)
- process wiring: no systemd unit / cron entry / CLI flag was added; a host must construct
  `AlertOutbox` + `Sender` (e.g. `WebhookSender`) and run
  `AlertDeliveryDaemon(...).run(stop=<Event>)` in a supervised process, setting the event on
  SIGTERM/SIGINT
- real target URL/token/idempotency behavior: host-owned; transport retry semantics rely on the
  endpoint honoring the `Idempotency-Key` to avoid duplicates after an ack-time crash
- production config and deployment: intentionally not touched
