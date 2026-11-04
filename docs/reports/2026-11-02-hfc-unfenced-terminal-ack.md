# HFC terminal ACK regression: unfenced events

All incident timestamps below are server-local UTC+08:00 on 2026-11-02.

## Correlated observation

The 15:37 and 15:45 user turns each produced a streaming card, reached
`message.completed` in the sidecar, and left a `completed/delivered` session.
Nevertheless Gateway entered its normal final text send. Thus the earlier
explanation that every duplicate was a missing completion event was insufficient.
The Gateway's `Normal final-send NOT suppressed` warning runs before the HFC
completion decision and is not, alone, proof that HFC suppression failed.

## Reproduced defect

In the previously installed repair, `_events` waited for the terminal platform
update but projected the post-delivery outcome into the HTTP response only inside
`if fence_claim is not None`. A legacy event without `event_id` therefore returned
the pre-update `delivery.outcome=unknown` even after a confirmed PATCH. Gateway's
strict outcome check correctly refused to suppress native text on that response.

The new parameterized regression fails on the previous sidecar for the unfenced
case and passes for a fully identified event. The candidate moves outcome
projection outside the fence-only branch, preserving the same response in the
idempotency record when a fence exists. It does not reinterpret unknown as success.

Patch: `patches/hfc-delivery-safety/terminal-ack-no-event-id.py.patch`.
Apply after the existing delivery-safety server patch, not directly to upstream.

## Verification

- Previous repaired source: successful-terminal test **1 failed, 1 passed**.
- Candidate, isolated Python 3.12: **8 passed** delivery contracts.
- Ordinary wheel installed into dedicated Python 3.14 environment: **8 passed**.
- Includes unfenced/fenced success, bounded unknown on blocked PATCH, definite
  failure control, late answer, duplicate terminal and failed first create.
- Live sidecar replay of an already delivered terminal without event identity:
  `ok=true, applied=true, delivery.outcome=delivered`; zero additional platform
  create/PATCH attempts. This is a terminal replay, not a fresh model/user turn.

## Deployment and limits

At 15:47:04 the HFC-owned CLI service was switched to the ordinary candidate
wheel using supported `service.python_executable` / `service.hermes_home` config
and CLI disable/enable, after an idle check and private rollback snapshot.
No Gateway restart or additional installed-source edit was made in this cutover.
The latest observed duplicate at that point was the pre-cutover 15:45:30 send.

Wheel SHA-256:
`861c7c7ec23a198e19fbe7911d760dc3b675b838504638cb7a8bf7ec7eee8bf2`.
Server SHA-256:
`c046ad146258227dc6bfaf01fad1c957b0f8f9035e813c5d8b26bca9ae62c238`.

Private environment: `~/.hermes/tools/hfc-terminal-ack-20261102/venv`.
Private rollback snapshot: `~/.hermes/backups/hfc-terminal-ack-20261102T154656`.
Rollback uses HFC CLI disable, restores the saved config, then enables through
the previous HFC CLI/runtime; never copy ownership manifests alone.

Doctor reports supported Hermes with **partial compatibility**, integration
installed, import/SDK OK; service readiness is healthy/ready. This is not full
compatibility or end-to-end acceptance. Existing manual Gateway-side hotfixes
from earlier attempts remain in place and are not legitimized by this patch.
In particular the admission bypass needs separate review, and initial `/proc`
environment contents alone cannot establish whether dotenv values are used.

A fresh post-cutover user/model turn, client rendering, attachments/callbacks,
crash/restart recovery and exact Git-source full-suite acceptance remain unverified.
The integration release hold remains in force. Do not claim that all duplication
or loss paths have been repaired based on this narrow regression.
