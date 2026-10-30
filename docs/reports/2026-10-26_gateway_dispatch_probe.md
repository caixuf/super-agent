# Gateway durable-dispatch probe: source-verified procedure

Date: 2026-10-26. This report is based on the running host's current Hermes
source. It does not claim that a model turn or an outbound delivery was tested.
No credentials were read or printed, no model POST was sent, and no configuration
or service was changed by this inspection.

## Observed listener

Read-only requests to `http://127.0.0.1:8642` returned:

| Request without credentials | Result |
| --- | --- |
| `GET /health` | HTTP 200, keys `platform`, `status`, `version` |
| `GET /v1/capabilities` | HTTP 401 |
| `GET /v1/toolsets` | HTTP 401 |

The implementation lives under `gateway/platforms/api_server.py` and
`api_server_openai_routes.py`, rather than a standalone platform-plugin folder.

## Authentication and profile selection

The adapter uses a Bearer key. Its internal `extra.key` may be populated from
merged platform configuration (`gateway.api_server.key`, `platforms.api_server`
or nested `extra`), or from the profile-scoped `API_SERVER_KEY` secret.
Do not assume that reading a single YAML path identifies the effective key.

The host's public scope builder is
`agent.secret_scope.build_profile_secret_scope(profile_home)`. It composes the
profile `.env`, external secret sources and managed secret values. A trusted
probe may resolve `API_SERVER_KEY` in process through that API, construct an
Authorization header in memory and print only status and sanitized results.
Do not put the key in shell arguments, generated reports or full request logs.

The optional `/p/<profile>/` URL prefix selects a served profile. A prefix naming
another profile is rejected on a single-profile gateway. Use the current
profile's endpoint, and keep the same prefix on all later requests. Do not guess
another profile or substitute its authentication.

## Tool visibility gate

Before sending a model request, inspect authenticated `GET /v1/toolsets`. The
response is:

```json
{"object":"list","platform":"api_server","data":[
  {"name":"durable_dispatch","enabled":true,"configured":true,
   "tools":["durable_cancel","durable_status","durable_submit"]}
]}
```

Only this relevant row needs to be retained. Actual registry discovery can
include unrelated tools; do not publish that full inventory unnecessarily.

`APIServerAdapter._create_agent` resolves its tools through
`_get_platform_tools(user_config, "api_server")`. A body-level OpenAI `tools` or
`tool_choice` field does not install or force the Hermes plugin toolset. The
toolset must exist in the current profile registry and be enabled for the
platform. `agent.disabled_toolsets` is applied last and can suppress it.

Public plugin enable uses `_toggle_plugin_toolset`: it appends the actual toolset
key to already-saved platform lists. A platform without a saved list can inherit
new plugin tools by default. Do not replace the platform list with just
`durable_dispatch`; that would remove existing built-in tools. Verify through the
endpoint after activation. New tool schemas should be exercised in a fresh
session, preserving existing session prefixes.

## Recommended fresh-session smoke sequence

1. Generate a unique identifier such as `durable-probe-<uuid>`. Never use an
   existing user conversation ID.
2. With authenticated HTTP, create an empty API session using
   `POST /api/sessions`:

   ```json
   {"id":"durable-probe-<uuid>","source":"api_server",
    "title":"Durable dispatch probe"}
   ```

   Expect HTTP 201 and `{"object":"hermes.session","session":{"id":...}}`.
   This endpoint accepts `id` or `session_id`; `source` defaults to `api_server`.
   Do not add room, bot-chat, platform-event or relay fields.
3. Send `POST /v1/chat/completions` with headers:

   - `Authorization: Bearer <resolved in process>`
   - `Content-Type: application/json`
   - `X-Hermes-Session-Id: durable-probe-<uuid>`
   - `X-Hermes-Session-Key: durable-probe:<uuid>`
   - `Idempotency-Key: durable-submit-probe-<uuid>`

   Use `stream: false`, the alias from `GET /v1/models` (or the gateway virtual
   alias `hermes-agent`), and a minimal instruction to call `durable_submit` once
   with an administrator-approved synthetic evidence file. Request only the task
   ID and a brief acknowledgement, with no polling, shell commands, analysis in
   the chat turn or artifact generation.
4. Require HTTP 200, `choices[0].finish_reason == "stop"`, nonempty assistant
   content, and no false `hermes.completed`, partial, failed or error indicators.
   Save the response `X-Hermes-Session-Id`; compression can resolve it to a live
   continuation internally while preserving the stable caller-facing header.
5. Check only this probe's messages with
   `GET /api/sessions/{id}/messages?order=oldest&limit=100`. It returns
   `{"object":"list","session_id":...,"data":[...],"pagination":...}`.
   Confirm a real `durable_submit` tool call and successful result. A model
   inventing a task ID in prose is not evidence that dispatch occurred. Retain
   only the relevant tool names, IDs, statuses and selected sanitized text.
6. In the same explicit session, ask a short ordinary question while the worker
   is still running, if the test workload actually lasts long enough. Confirm
   prompt response independently of worker completion. If the worker has already
   completed, record that this case did not test overlap rather than inventing a
   timing claim.
7. Use a new idempotency key for a later `durable_status` query in the same
   session. Confirm the queued job's owner isolation, terminal status and checked
   report references. Reusing the submit key with changed content is a conflict,
   not a fresh status request.

An alternative first request can omit `X-Hermes-Session-Id`. Hermes derives a
stable session from the system prompt and first user message and returns its ID.
Explicit pre-creation is easier to audit and avoids accidental fingerprint
collisions. A session-key header scopes memory; it is not the session ID.

## Transport boundary

`APIServerAdapter._bind_api_server_session` explicitly binds
`platform="api_server"`, `async_delivery=False`, and history delivery denied
unless the route declares session continuation. A normal API turn therefore
does not inherit another messaging transport as its callback destination.
The probe should use ordinary API routes and no external platform event, relay,
room or notification metadata. That boundary does not prevent a model from
choosing an unrelated explicit outbound tool, so the prompt must remain narrowly
scoped and the tool trace should be checked.

## Result interpretation

Non-streaming Chat Completions uses the standard `choices[].message.content`
envelope and can add `hermes` status metadata. Failure with no usable text may
return HTTP 502. A partial run may still return HTTP 200 with
`finish_reason="length"` or `"error"`; HTTP 200 alone is insufficient.

The API and worker have different task records. A completed chat turn proves
that it handed work off or answered a query. It does not prove that the analysis
job succeeded. Conversely, a completed worker record does not prove the main
conversation remained responsive. Keep these two assertions separate.

## Source anchors

- `gateway/platforms/api_server.py`: adapter key initialization, `_check_auth`,
  `_handle_toolsets`, `_handle_create_session`, `_handle_session_messages`,
  `_bind_api_server_session`.
- `gateway/platforms/api_server_openai_routes.py`: `_handle_chat_completions`,
  explicit versus derived session identity, response status and headers.
- `hermes_cli/tools_config.py`: `_get_platform_tools` and plugin-toolset rules.
- `hermes_cli/plugins_cmd.py`: `_toggle_plugin_toolset` preserves saved lists.
- `agent/secret_scope.py`: `build_profile_secret_scope`.
- `website/docs/user-guide/features/api-server.md`: public API semantics.

This inspection produced a plan and three unauthenticated GET observations.
Authenticated tool visibility, dispatch, overlap, cancellation and worker
completion are still the main implementer's live validation steps.
