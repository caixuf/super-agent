# Background completion delivery

The old consumer injected a user message to report completion. That started
another model turn, and its acknowledgement proved only injection acceptance.
The optional direct transport now resolves the persisted route through the
host's profile ownership checks and uses the answering platform adapter.

Worker protocol envelopes are unwrapped before rendering the summary. Native
attachments are snapshotted from the job's actual attempt directory before any
part is sent. Other attempts, symlinks and oversized files are refused.

Each summary and file has a separate durable operation journal. Real platform
message IDs acknowledge parts; only all-part success acknowledges the queue.
Restart and lost queue ACK reuse successful receipts. A send with an unknown
outcome is fenced rather than automatically repeated. A definite refusal
before sending may retry. This deliberately prefers an operator-visible pending
delivery over duplicate messages; it does not promise exactly-once delivery.

Feishu requires the supplied stable-UUID patch, including its runtime marker.
It keeps the same operation UUID across the adapter's own retries. The bridge
refuses an unpatched adapter and does not count local viewer notices as
messaging-platform delivery.

`delivery_not_before` is an optional submission-time cutoff. Older receipts stay
pending without blocking newer ones, so enabling a sender need not flood a
conversation with historical completions. The cutoff is enforced by the
canonical queue claim query, not by deleting or acknowledging old receipts.

An uncertain or temporarily refused direct send is deferred for the current
drain pass, without acknowledgement. Later independent completions still flow;
the deferred task cannot block the entire queue.

Verification: the deployment-host verify script passed 767 tests with 9 optional
native-binding skips, plus shell syntax and installation dry-run checks. A real
Hermes PluginContext and route resolver with a fake platform adapter sent one
summary and one file, acknowledged the outbox and started zero model turns.
This is host integration evidence, not a real platform delivery claim. Real
platform acceptance still requires a newly submitted user task. Linking the
completion to its original task card is also a separate acceptance boundary;
this transport does not guess a last-seen card.
