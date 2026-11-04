# HFC v5.0.13 delivery-loss repair

Status: **candidate source repair, not deployed to a live HFC installation.**
Base: HFC v5.0.13 `e072763f1b6e3170a24be70fc7474df05ee0552f`.

This directory is a reviewable upstream-shaped patch. It does **not** modify a
running Hermes/HFC checkout. Use HFC's supported installer and an isolated source,
HOME and state directory for any installation or wheel build.

## Corrected contracts

1. **Ownership is not delivery.** Terminal HTTP acknowledgement waits for the
   bounded terminal task and returns `delivery.outcome=delivered|not_sent|unknown`.
   The idempotency fence stores that post-delivery result, not the pre-PATCH state.
2. **No fake delivery proxy.** Hermes suppresses its native final only for an
   explicit `delivered` result. `not_sent` and `unknown` cannot become success.
3. **Sidecar-owned text recovery.** If the original PATCH and replacement-card
   create fail, the sidecar posts the normalized complete answer as text with the
   same stable UUID. This does not depend on a second Hermes model turn.
4. **Stable text retries.** `FeishuClient.send_text_message` carries `delivery_uuid`
   on send/reply, uses the same bounded retries as card creation and validates the
   UUID before I/O.
5. **Late terminal delivery.** A superseded/failed display is not a receipt. A late
   answer is delivered when its normalized text was not already confirmed; exact
   duplicates remain idempotent.
6. **Compatibility.** Old two-key `{"ok":true,"applied":true}` responses remain
   readable during rollout; new terminal responses use the explicit outcome.

## Evidence

Run against v5.0.13 source with Python 3.12 in an isolated HOME/state:

- `tests/integration/test_terminal_text_fallback.py`: 4 passed.
- External safety contract in super-agent
  `tests/test_hfc_delivery_contract.py`: 5 passed, including all three formerly
  failing loss modes and normal/duplicate controls.
- HFC text-send HTTP tests: 10 passed, including stable UUID on send and reply.
- Related HFC unit suite: 645 passed.
- HFC upstream terminal-focused integration suite currently has four **expected
  failures** because those tests encode the old contract “ack success even when
  card PATCH/recovery failed”. They must be updated/replaced as superseded by the
  new delivery contract before release; they are not claimed green.

Not covered here: an exact clean Git fixture full-suite run, ordinary wheel build,
installed-runtime doctor, a real Feishu API call, real client screenshots and a
real Gateway/model turn. The earlier source archive was Git-blob verified but HFC
preflight correctly refused it as `wrong_repository`; no full-fixture gate is
claimed.

## Required deployment gate

Use a private source checkout, private HOME/profile/state and a dedicated test bot.
Build an ordinary non-editable wheel from the repaired source through HFC's normal
install transaction. Run HFC doctor and integrity checks before starting. Then run
these live checks before switching the production platform:

- first card create failure → native text still delivered;
- terminal PATCH failure → replacement card or complete text delivered once;
- forced timeout recording `unknown` → no blind duplicate, reconciliation visible;
- duplicate terminal/replay → no second answer;
- superseded then late terminal → unseen answer delivered;
- topic reply, clarify/approval callback, media attachment and reconnect;
- two consecutive long turns and a queued follow-up;
- exact rollback to the v5.0.13 backup and restoration of service health.

Do not hand-edit an installed Hermes target, copy these files into a live tree,
or enable this beside another card producer. This candidate does not lift the
super-agent HFC release quarantine until its lock is updated from reviewed,
exact-revision evidence.

## Follow-up: terminal response without event identity

`terminal-ack-no-event-id.py.patch` applies **after** this directory's server
repair. It fixes a stale `unknown` response on successfully delivered legacy
terminal requests without `event_id`. Regression and a narrow sidecar deployment
are recorded in `docs/reports/2026-11-02-hfc-unfenced-terminal-ack.md`.
The original candidate-only status above describes the earlier investigation;
this follow-up does not clear the integration release hold or certify the
pre-existing manual Gateway hotfixes. Fresh user-turn acceptance is still needed.
