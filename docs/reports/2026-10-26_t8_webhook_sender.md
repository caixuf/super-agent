# T8 webhook sender — 2026-10-26

## Status
DONE (new files only; default-off; no commit, no production config, no external network).

## Files
- `agentcore/ops_alert_senders.py` (new)
- `tests/test_ops_alert_senders.py` (new)

## API
- `WebhookSender(url, *, token=None, timeout=5.0)`
  - constructor validates: non-empty absolute `http(s)` URL without embedded credentials,
    non-empty single-line token, finite positive `timeout`; no env reads, no background task
  - `send(payload, *, idempotency_key) -> DeliveryReceipt | None`
    - `POST` UTF-8 JSON (`sort_keys=True`), headers `Content-Type: application/json`,
      `Idempotency-Key: <idempotency_key>`, optional `Authorization: Bearer <token>`
    - accepts only explicit `2xx` **and** a receipt id: JSON `receipt_id` (preferred),
      JSON `message_id` (fallback), else `X-Receipt-Id` response header; string/int only,
      blank/boolean/numeric-float rejected
    - success -> `DeliveryReceipt(accepted=True, event_id=<idempotency_key>, receipt_id=<id>)`
    - non-2xx, redirect, timeout/connection error, malformed/empty body without receipt -> `None`
      (outbox leaves the event pending and owns retry)
    - no retries in the sender; redirects refused (`_NoRedirect`) so the bearer token cannot be
      forwarded to another host
    - token is never logged, printed, or included in a receipt; `repr()` redacts it to `<set>`
    - non-mapping payload / blank key / null bytes raise `ValueError`; unserializable payload
      raises `TypeError` (caller bug, surfaced by `deliver_once` as `delivery_unknown`)
- Constants: `RECEIPT_HEADER = "X-Receipt-Id"`, `MAX_RESPONSE_BYTES = 64 KiB`

## Configuration keys (documented in the module docstring; nothing is bound)
| key | required | meaning |
|---|---|---|
| `url` | yes | absolute `http`/`https` endpoint |
| `token` | no | bearer token, never logged |
| `timeout` | no | transport timeout seconds, default `5.0` |

Default-off: no caller in the repository constructs `WebhookSender`; the guard/outbox still
receive `sender=None` unless a host explicitly builds one. No env vars are read; a host may map
`OPS_ALERT_WEBHOOK_URL` / `OPS_ALERT_WEBHOOK_TOKEN` / `OPS_ALERT_WEBHOOK_TIMEOUT` itself.

## Verification
```
ssh 远端宿主
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_ops_alert_senders.py -q
# 50 passed in 3.49s
TMPDIR=/dev/shm python3 -m pytest tests/test_ops_alert_outbox.py tests/test_capability_guard.py tests/test_completion_delivery.py -q
# 60 passed in 0.42s
python3 -m py_compile agentcore/ops_alert_senders.py tests/test_ops_alert_senders.py
# COMPILE_OK
```

Coverage includes: accepted JSON `receipt_id` / JSON `message_id` / numeric id / padded id /
`X-Receipt-Id` header; 301/302/400/401/429/500/503 with a receipt still -> `None` and one request
only; timeout -> `None` without retry; 12 malformed-2xx variants -> `None`; idempotency header on
the wire; bearer token on the wire but absent from `caplog` and `repr`; redirect to a second local
server never receives the token; constructor validation for URL/token/timeout; and two outbox
integration cases (confirmed delivery acks and clears pending; 500 keeps the event pending with
`attempts == 1`, no `receipt_id`). All traffic is loopback `http.server` in a daemon thread.

## Remaining (deliberate)
- real target URL/token: host must supply them; no production config was added
- wiring to the guard CLI (`capability_guard.run_guard(..., sender=...)` / a CLI flag) and a
  scheduled delivery cycle; currently no caller constructs the sender
- endpoint-side idempotency (`Idempotency-Key`) must be honored by the real target to avoid
  duplicates after an ack-time crash
- TLS/auth policy for the real target (custom CA, mTLS) is host-specific and not implemented
