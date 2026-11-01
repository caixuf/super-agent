# Progress messages on the deployed Hermes host

Status: implemented; deployment acceptance is recorded separately below.

The deployed Hermes 0.22.2 host has no `gateway.plugin_messages` module and no
`PluginContext.post_message`. The progress poller repeatedly raised ImportError
while opening foreground progress. Mocking a post_message method into the host
in earlier tests hid this incompatible assumption.

Choice: keep the compatibility seam inside the task-progress plugin.
`_message_transport()` prefers the native host API; otherwise it loads the
plugin's `legacy_messages.py`. Core agentcore code gains no host/CLI dependency.
The fallback delegates existing-session lookup, profile canonicalization,
authorization and adapter selection to host methods, and fails closed when
those methods are unavailable. It only sends through Feishu/Lark, rechecks
authorization before posting, and supplies expect_edits for card dispatch.
It never injects model input, creates a session or uses send-on-edit fallback.

Alternatives rejected: upgrading/replacing the whole host, monkey-patching a
missing global host API, or injecting a model turn to produce progress.

Verification: legacy transport route/error/blank-content checks and existing
task-progress checks pass. A separate integration test instantiates the actual
installed GatewayRunner, PluginContext, SessionStore and FeishuAdapter in an
isolated home. Only adapter send/edit network calls are mocked. It posts one
message to a stored session, edits the same ID, and refuses an unserved profile.
No post_message method is supplied by the test.

Empty-message investigation is separate: read-only platform readback found
nonempty content in the recent completed reply and no undeleted empty text/post
among the most recent 50 records. Interactive-card readback provides only a
reduced representation, so it cannot disprove a client rendering problem.
No speculative stream-finalization patch was applied; a specific empty-message
timestamp, ID or screenshot is needed to correlate that remaining symptom.

## Activation

113 focused card/config/progress/host-integration checks passed. An independent
read-only review reran the nine legacy unit/installed-host checks successfully.
The two runtime plugin files were backed up/deployed with before-hash checks;
the existing plugin was older than the current release and was updated to the
reviewed source. No other plugins or host modules were changed in this step.
Following the user's interruption/restart authorization, one service restart
activated the plugin. The new process is active/running, reports NRestarts=0,
and the platform is connected. The new process's startup log has no missing
plugin-message import error. Installed hashes match deployed source; protected
configuration, identity and credential hashes remain unchanged.

These checks do not claim a real user-initiated long-task acceptance test has
completed, nor that the separately reported visually empty message is fixed.
