# HFC cutover / rollback runbook for bot1

## Deployment result — 2026-11-01 11:30 UTC

HFC v5.0.13 is now active on bot1. This is infrastructure acceptance, **not**
proof that the original empty-message/client interaction failure is resolved.
A real user turn, tool update, final rendering and button callback still need
client acceptance under the criteria below.

- `super-agent` fetched and rebased onto `origin/main`; no conflict or pending
  source changes existed before recording this deployment.
- Ordinary non-editable HFC wheel built from the exact upstream commit below;
  install provenance retains the pinned Git revision. CLI environment:
  `/home/caixuf/.hermes/tools/hfc-5.0.13` (not the temporary test environment).
- Sidecar config: `/home/caixuf/.hermes/hfc-bot1.yaml`; credentials remain in the
  existing private `.env`, never in this repository. Sidecar listens on loopback.
- Setup used explicit source, HOME, config, env and default profile, with
  `--skip-start --no-repair` and an exact `HFC_INSTALL_SPEC`. The first attempt
  refused missing install provenance; supplying the pinned spec succeeded.
- HFC's installer registered its runtime dependency through Hermes PM, installed
  its managed hooks, and verified SDK capability. No installed source was edited
  manually. All four pre-existing custom host file hashes remain unchanged.
- `streaming.enabled: true`, `streaming.transport: edit`; native Feishu
  `card_replies` and `exec_approval_text` are false; task-progress's documented
  `settings.enabled` is false. Task-status remains enabled.
- Persistent `hermes-feishu-card-sidecar.service` enabled using HFC's CLI owner;
  `hermes-gateway.service` restarted once through systemd, then reconnected.
- Doctor: compatibility full, runtime import OK, SDK OK, install state installed
  and consistent. Health: healthy, live delivery, runtime_ready, safe integrity,
  matching generation, no owner conflict and no restart required.
- Both units active/running with `NRestarts=0`. No post-start warning, import,
  duplicate-delivery or error log entries were observed during the initial check.
  Zero send failures before a user turn is not evidence that delivery is fixed.

Private rollback snapshot:
`/home/caixuf/.hermes/backups/hfc-cutover-20261101-xlWsia` contains original config,
env, gateway unit/drop-in, custom host patch/hashes, PM facts and inputs. Do not
commit this directory. Preserve any later edits before restoring its contents.
Stop/disable only the HFC-owned service through its CLI, uninstall with explicit
Hermes source/HOME and `--yes`, then restore the saved bot1 configuration as
described below. Do not restore PM facts alone or reset the Hermes checkout.

Validation used installed-runtime doctor, bounded health/config assertions,
original custom-file hash comparison, systemd state and post-start logs. The
earlier isolated test totals below were not rerun or claimed as a new full suite.
Preflight's full-fixture gate remains incomplete because the available v0.22.2
fixture is not its separate v2026.8.3 canonical fixture.

## State before cutover

- Hermes: v0.22.2 `d923a5eb487a17c0a68801bba9ef0f56c81040a8`.
- HFC candidate: v5.0.13 `e072763f1b6e3170a24be70fc7474df05ee0552f`.
- HFC source compatibility: full; all 19 anchors found.
- HFC mock/interaction suite: 678 passed with a private HOME/state.
- Real Feishu direct card create + CardKit PATCH smoke: passed through bot1's
  existing application credentials. The smoke proves permissions and API
  acceptance, not a full Agent turn.
- Existing producers: bot1's host card patches, task-progress cards and other
  plugin notifications. Running all of them with HFC risks duplicate cards or
  contradictory native suppression.

## One-producer cutover

Do this only in an idle maintenance window after taking hashes/backups.

1. Install the published HFC v5.0.13 wheel through HFC setup/install against
   the real checkout/HOME. Do not hand-edit Hermes source or copy a stale test
   manifest. Verify HFC doctor status, all owned targets, runtime import, SDK
   and recovery state before restart.
2. Disable the task-progress plugin's message producer with its documented
   setting; retain task status/command tools if desired. Do not leave it posting
   a second progress card.
3. Turn off bot1's native Feishu `card_replies` path so HFC owns reply cards.
   Keep `streaming.enabled: true` and `streaming.transport: edit`, which HFC
   requires. Reconcile unrelated plugin notifications explicitly; silence is
   preferable to duplicate delivery if ownership is unknown.
4. Keep Hermes source patches installed until the cutover proves green: they
   alter shared stream/final delivery and HFC refuses unknown drift. A later
   cleanup must be a separate, reversible change. Do not stack another adapter
   card implementation on top of HFC.
5. Restart sidecar and Gateway through their service owners, verify authenticated
   readiness and a new Feishu connected generation, then run a private-chat
   real model/tool turn.

## Acceptance evidence

Pass requires one visible card per turn with nonempty first content, stable
in-place updates across tools, final content retained, one terminal state and
no duplicate native text. Confirm a button/clarification round trip resumes the
same turn, reconnect changes no route/bot, and the bot1 home chat never sees
another instance's replies. Capture redacted health counters and client
screenshots; do not record credentials, tokens or raw ids.

## Rollback

Stop only the HFC-owned sidecar, run HFC's verified uninstall/restore path, then
restore the saved bot1 config/plugin state and restart the same Gateway service.
Never overwrite an unknown post-cutover edit or execute a repository reset.
Confirm pre-cutover config hashes, legacy patch checks, Gateway active/running,
zero unexpected restarts and platform connected before declaring rollback done.

The cutover is not complete until live client evidence exists. Source tests and
direct smoke cannot prove visual rendering, first-frame behavior or callback
click semantics.
