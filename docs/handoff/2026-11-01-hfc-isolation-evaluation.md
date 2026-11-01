# HFC v5.0.13 isolation evaluation

Status: activated on bot1 at 2026-11-01 11:30 UTC after the evaluation below.
Gateway/sidecar readiness passed; real-client turn and button acceptance remain
pending. See the cutover runbook for deployment and rollback evidence.

## Why evaluate

The existing card integration has accumulated Hermes-source patches, adapter
hooks, update recovery and a separate task-progress transport. The user asked
whether an existing Feishu interaction project should replace that hand-built
surface. `hermes-feishu-streaming-card` (HFC) is the closest candidate: it is
designed for Hermes, covers live cards/tool state/interactions, and has
transactional installation and recovery.

## Version and compatibility evidence

- Evaluated annotated release tag `v5.0.13` (`e072763f1b6e3170a24be70fc7474df05ee0552f`).
- Bot1 runs Hermes `v0.22.2` at
  `d923a5eb487a17c0a68801bba9ef0f56c81040a8`.
- HFC's CI explicitly pins that same commit; its source fixture hashes all 12
  tracked files exactly.
- Read-only `doctor` against bot1's live source reported `compatibility: full`,
  all 19 anchors found (`gateway_run_013_plus`, exact Base delivery, cron).
- A source copy of bot1's current modified checkout also reported full
  compatibility before any HFC mutation.
- Installing on that source copy proceeded through both stock and custom
  changes, reporting `install ok`, then `doctor` reported installed and all
  runtime imports available. This proves the existing card files do not create
  an immediate anchor collision, not that both card producers should remain.

## Isolated test evidence

- HFC isolation environment: Python 3.12, ordinary editable checkout.
- 163 focused installer, HTTP/mock Feishu, manifest and SDK tests passed;
  52 tests requiring unrelated pinned upstream source fixtures were skipped.
- Exact v0.22.2 source transaction run: 5 passed, 41 skipped in 100.96s.
  Install, repeat install, doctor, integrity migration and byte-for-byte
  restore/removal passed.
- The fixed-source evidence required its own checkout, separate from the
  deployed host, and source hashes were checked before execution.

## Isolation defect found

The first setup attempt resolved the Hermes PM runtime from the selected
`--hermes-home`. Although the source was a temporary copy, that HOME was the
real bot1 home. It registered HFC and a code-free dependency member in the
real PM/config before the run was stopped. Nothing was started and the running
Gateway was not restarted, so the new paths were not loaded.

Cleanup: the HFC uninstall transaction restored the temporary source, removed
the main config registration and owned dependency directory, and removed the
PM lock/member declaration. The HFC-owned extra uncommitted PM environment and
wheel cache were then checked to be outside bot1's selected runtime and are
being reclaimed as test artifacts. Bot1 remains on its pre-test committed
environment and active/connected.

Verified cleanup: a pre-test config backup created after the first HFC
registration was removed (the 18:50:07 file still contained the dependency).
The exact pre-HFC production config is the 18:52:34 backup; the live config is
byte-identical to it. Current `facts.json` points to the pre-test committed
environment, not the temporary generation. No HFC feature plugin is enabled;
the Gateway PID stayed 3168154, active/running with zero restart count, and
Feishu remained connected. Temporary test wheels/environments/caches remain
unreferenced test artifacts and are safe to reclaim after rechecks.

Future isolation mandates a private HOME, private source checkout and explicit
private profile/config instead of defaulting to production. A stale test
checkout must never be trusted for a later activation.

## Decision and remaining gate

HFC is a credible replacement candidate, but activation is intentionally held.
Before production, test a private bot application/chat (separate credentials or
an isolated test event source), including first-frame content, no empty message,
tool progress, buttons/clarification, final answer retention, reconnect and
multi-instance routing. Also decide whether to disable bot1's existing card,
task-progress and adapter-card producers; running both would duplicate cards
or fight over native suppression. No such switch has been made.

## Further acceptance evidence

- 678 server/interaction/card/HTTP-focused HFC tests passed in 141.28s using
  a private HOME and HFC state. This expands beyond installation into first
  cards, content validation, in-place updates, retries, interaction
  continuation, terminal states and empty-card indexing behavior.
- A direct real Feishu smoke card was created and PATCHed successfully using
  bot1's existing app credentials and one private chat. No id, secret or
  answer text is retained here. This proves API permission and CardKit update,
  not Gateway/model interaction or visual client acceptance.
- A one-producer cutover/rollback runbook is recorded in
  `2026-11-01-hfc-cutover-runbook.md`. The existing task-progress and native
  card producer must not remain active beside HFC without an ownership test.
