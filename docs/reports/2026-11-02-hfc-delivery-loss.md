# HFC v5.0.13 message-loss investigation

Date: 2026-11-02. This is source-level fault-injection evidence, not a claim that
every user-observed missing card has the same cause or that production is fixed.
No live HFC install, configuration change, service restart or Feishu test message
was performed.

## Scope and provenance

- HFC pin: v5.0.13, `e072763f1b6e3170a24be70fc7474df05ee0552f`.
- Source obtained from that commit's Git tree and raw files; all 415 downloaded
  source/test/document blobs verified against their Git blob SHA-1, with no
  mismatches. This is an archive-style partial source tree, not a clean Git
  fixture. HFC preflight correctly refused it as `wrong_repository`; no full
  fixture, install-matrix or release-validation pass is claimed.
- Existing local caixuf Gateway: active, Hermes v0.22.5,
  `bf0203a794a07aac3ff764972ab9087baa4e4f4e`, legacy/native card replies enabled,
  busy input queue enabled. HFC was not present in this local installation.
  Historical bot1 cutover documentation refers to a different v0.22.2 host.
- Local Hermes already has uncommitted custom patches. No patch/config/runtime
  was overwritten and no production bot credentials were read.
- Testing used Python 3.12, isolated HOME/HERMES_HOME/HFC state, a fake Feishu
  transport and actual aiohttp loopback requests. No model or real Feishu API
  participated.

## Confirmed mechanisms

### 1. Accepted terminal state is mistaken for delivered terminal content

`server._apply_event_locked_inner` schedules the terminal FlushController task,
then returns `ok=true, applied=true`. `server._should_await_card_update` always
returns false. Consequently `/events` may acknowledge before the final PATCH.
The event-id fence is also finalized before that background delivery completes.

`hook_runtime.prepare_exact_base_final_delivery` treats that acknowledgement
as delivered ownership, substitutes `_ExactCardDeliveryAdapterProxy`, and its
`_send_with_retry` returns success without invoking the original native sender.

Deterministic test: hold the platform PATCH on an asyncio Event. The HTTP
response already says applied while no final card update has occurred. This
alone is a premature-ack defect; the next test establishes actual lost output.

### 2. Definite delivery failure still looks successful to Hermes

Inject rejection for every terminal PATCH and for the recovery card create
(`outcome=not_sent`). After recovery completes:

- session `terminal_delivery_state` is `not_sent`;
- only the initial placeholder card exists, no final card update exists;
- Hermes' selected delivery adapter has already returned `success=True`;
- the native final-answer sender was never called.

The error is observable inside sidecar diagnostics but cannot undo the native
success already returned. Existing upstream tests explicitly accept terminal
ACK before a slow PATCH and assert no native fallback even when recovery fails;
those tests therefore do not enforce an end-to-end delivery guarantee.

### 3. Display supersession is treated as proof that a late answer was handled

`_abandon_stale_sessions_for_chat` marks a prior same-conversation display
failed when a new lifecycle starts. A later completed event for that display
falls into `terminal_already_handled`: terminal status alone makes it return
`applied=true`, without delivering or comparing that late answer.

Test: start A, start B in the same route, then complete A with a unique marker.
HFC acknowledges A but no sent/updated card contains its answer. This is a
source-proven suppression path, not evidence that the production Gateway really
executed concurrent A/B turns or that an intentional cancellation should resume.
A proper fix must distinguish cancellation policy from verified final delivery.

## Evidence and limits

`tests/test_hfc_delivery_contract.py` executes five external-source contracts:

- failing: terminal ACK precedes blocked platform delivery;
- failing: definite terminal failure still returns success to Hermes;
- failing: unseen late answer acknowledged after supersession;
- passing: normal complete answer and duplicate terminal idempotence;
- passing: failed first-card create does not acknowledge delivery.

Observed result against the unmodified pin: **3 failed, 2 passed**. The failures
are intentional safety assertions, not xfails and not repaired behavior.

Five initial upstream integration cases also passed, including their existing
slow-ACK/failed-recovery behavior. That does not contradict the new failing
contracts; the upstream assertions encode a different acceptance criterion.
A broader focused run of upstream terminal/queue/start cases completed with
**233 passed, 535 deselected**. This is not the full HFC test suite.

Repository gate/neutrality validation: **24 passed, 5 explicitly skipped** when
no external HFC source was selected. The same five external-source tests were
then executed separately with the **3 failing / 2 passing** result above.
A wider run including installer tests produced **1 failed, 48 passed, 5 skipped**:
the untouched installer test expected a missing-CLI message, while the locally
available Hermes CLI led to a missing-install-tree message. Neither that test nor
the installer was edited in this investigation; that failure remains unresolved.
`git diff --check` passed.

Ordinary first-card rejection keeps native delivery fail-open in the inspected
path. Thus these tests do not prove the specific cause of every missing *first*
card. Possible policy timeouts, ingress binding failures and queue/turn identity
need one real affected turn's correlated evidence. The local legacy Gateway log
had DNS-failure entries; it is a different runtime and cannot diagnose bot1.
No production logs or affected inbound message were inspected.

## Version-alignment question

On 2026-11-02 the current stable Hermes release and repository HEAD were
checked independently:

| Component | Verified identity | Finding |
|---|---|---|
| Hermes Agent stable | v0.22.5 / `bf0203a794a07aac3ff764972ab9087baa4e4f4e` | Local install is exactly this tag; `hermes update` reports up to date. |
| Hermes `main` | `557254faa24e` visible on 2026-11-01 (moving branch) | Newer than stable, with unrelated skills/desktop/gateway work. Not a tested HFC release pairing. |
| HFC | v5.0.13 / `e072763f1b6e3170a24be70fc7474df05ee0552f` | Latest published release; this is the already-reproduced candidate. |
| Current caixuf runtime | Hermes v0.22.5 plus 13 locally modified tracked files and 16 enabled plugins | It is **not** the clean HFC distribution and not a clean v0.22.5 runtime. |

Therefore “update everything to latest” does not mean “Hermes and HFC both move
forward”: Hermes stable is already v0.22.5 and HFC stable is already v5.0.13. The
remaining version risk is **kernel/drift coupling**: the local runtime contains a
large custom Feishu adapter plus hand-applied delivery patches. A clean-tag vs
clean-main comparison is required before attributing missing cards to scheduling.

Version alignment cannot repair the source-proven fake-success paths. It can,
however, remove installed-source drift and drift-related duplicate/fallback
behaviour. Both checks are required; neither substitutes for the other.

A safe alignment exercise must use a private Hermes checkout, private HOME/profile,
private HFC state and dedicated bot, and preserve this runtime for rollback. The
five identities to pin are: Hermes commit/tag, HFC commit/tag, every enabled
plugin provenance revision (manual plugins must be recorded explicitly), profile
config digest, and the installed source manifest digest. A matching version string
alone is not sufficient.

## Containment implemented here

The pinned integration lock now records three known delivery blockers. The
default read-only acceptance gate refuses interaction acceptance even with
healthy doctor/readiness and positive started/answer/completed samples. The
infrastructure-only gate retains its narrower purpose but cannot report
a passed interaction gate. No external HFC package or installed Hermes file was
patched from this repository.

**This is quarantine and reproducible diagnosis, not a delivery fix.**
Deploying this unchanged pin to another live profile merely to reproduce would
knowingly expose that profile to the confirmed failure path, so it was not done.

## Repair and real-turn acceptance requirements

The upstream delivery owner must separate accepted/pending from platform
confirmed delivery. Merely waiting indefinitely for PATCH is not a solution:
keep a bounded request budget and preserve same-turn routing and idempotence.
A definite not-sent final must retain a durable retry/native-delivery obligation;
unknown results need stable delivery identity and reconciliation rather than a
blind second send. A failed/abandoned display is not a platform receipt.

Before deployment, require these contracts plus normal/failure/duplicate/late
event tests, timeout/crash/restart recovery and queue/turn identity coverage.
Use HFC's supported installer on a private source/HOME and a verified ordinary
wheel; test compatibility against the actual local v0.22.5/custom source. Do not
hand-edit HFC-owned Hermes targets or share another bot's HFC state/config.

For a future user reproduction, retain timestamp/timezone and affected inbound
message in a private channel, then correlate receive/dedup, queue/claim, canonical
turn/profile/generation, HFC admission, actual card create/PATCH, native decision
and delivery-ledger result. Public reports should contain hashes/fixed outcome
codes only, not credentials or chat contents. A service health check and a
completed turn sample cannot substitute for that evidence.
