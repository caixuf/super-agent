# HFC cutover / rollback runbook for bot1

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
