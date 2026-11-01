# Reply-card recovery

Two independent failures were found in the installed gateway:

1. A malformed nested tip branch in the Feishu adapter prevented plugin import.
   The existing card patch probe occurred inside the malformed branch and
   incorrectly reported success. The patcher now repairs that exact known
   fragment before trusting probes, checks source syntax even on no-op paths,
   and refuses to write a candidate with syntax errors. Unknown drift is not
   repaired speculatively.
2. The active profile enabled `platforms.feishu.card_replies` but disabled
   `streaming.enabled`. The adapter's reply-card dispatch requires the stream
   consumer's `expect_edits` metadata, so non-streamed replies bypassed cards.
   The configuration checker now rejects disabled global streaming, transport
   off, or a disabled platform override when reply cards are requested.

The deployment changed only the profile's streaming master switch and added
the service environment `HERMES_FEISHU_CARD_V2=1` for native table cards.
Machine-local configuration, credentials, sessions and backups are not
included in this repository. The original profile configuration was backed up.

A graceful restart initially waited for active work. The user subsequently
explicitly authorized interrupting that work and restarting immediately.
The replacement gateway is active/running, reports zero automatic restarts,
has the Card V2 environment, and reports the messaging platform connected.

Regression coverage includes malformed-tip recovery for pristine/0.22 shapes,
reapplication idempotence, refusal to write unknown syntax failures, and the
effective streaming configuration gates. An offline installed-host smoke check
also exercised real adapter card dispatch with mocked transport, native table
rendering, shared-card configuration and final green headers. No real model
call or user-channel message was sent by these checks; live message delivery
still requires a user-initiated conversation acceptance check.

Verification: 53 card/config/stream/neutrality checks passed, plus 106
installer/real-host continuation/memory diagnostic checks. The installed
configuration checker returns success for the corrected active profile.
