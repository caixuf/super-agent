# Core activation review

Date: 2026-10-26. Scope: read-only source and deployment review, plus temporary
isolated probes. No production data, configuration or services were modified.
No outbound messages were sent and no full test suite was repeated.

## Findings requiring attention

1. **Cancellation response can disagree with persisted state.**
   `DispatchService.cancel` unconditionally returns `cancellation_requested=true`.
   After an already-completed task, its response says completed/true, while the
   next `status` says completed/false. An isolated queue reproduced this by
   submit -> claim -> finish(completed) -> cancel -> status. The response should
   use the persisted cancel flag and explicitly report an already-terminal task.

2. **Unselected source text can block an otherwise permitted code range.**
   `_read_snapshot` scans the full decoded file for credential patterns before
   choosing the requested line range. In the configured source tree, requesting
   lines 1-30 of `real_runner.py` failed with `source_contains_sensitive_assignment`
   because of a match at line 1028. AST inspection identified a dynamic JoinedStr;
   no matched source contents or values were printed. The other three sampled
   executor/store/watchdog files accepted the same 1-30 range. Filter only the
   selected text that will be sent to the model, while retaining filename denials,
   byte budgets, file identity checks and the full-file SHA-256. Selected secrets
   must still be rejected. This removes irrelevant rejection without exposing
   excluded lines to the model.

These findings were reported to the parent before writing this report. Their
fixes and final integrated tests belong to the main implementation turn.

## Confirmed deployment boundaries

- The report root is a mount symlink. Dispatch normalizes both administrator roots
  and source paths, and the runner resolves its administrator roots before
  anchored openat traversal. The two sides therefore agree on the canonical path.
- Sample report logs are owned by the service user, readable, and about 80-90 KB,
  below the proposed 8 MiB file budget. The home/state filesystem is local ext,
  appropriate for SQLite WAL plus flock. Queue ownership must remain private.
- A normal worker stop stops admission and drains active attempts. The proposed
  service hard-stop deadline is 15 seconds, shorter than the 120-second task
  deadline; a slow model call may be killed and recovered after the lease expires.
  That is a visible recovery delay, not evidence of task loss. The role is
  replay-safe because it is read-only; do not reuse that policy for external writes.
- Process groups and directories are not an arbitrary-code sandbox. The configured
  runner has no model tools and invokes only the approved model callable.
- Completed status returns artifact references. Automatic message delivery and
  cross-session sharing are separate integrations; this review does not claim
  either is enabled. Per-session status/cancel ownership is intentional.
- The documented launch/PID-registration crash window remains conservative, and
  service-wide control-group cleanup limits surviving descendants on normal stops.

## Remaining acceptance

After the two response/source-selection issues are resolved, test one permitted
report through the actual independently managed worker, query it through the
correct session, cancel an active task, and restart only the new worker while
checking that the gateway PID remains unchanged. Real business diagnosis accuracy
and real user messaging behavior still require their own acceptance evidence.
