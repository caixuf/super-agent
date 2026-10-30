# CCP read-only analysis adapter

`agentcore.ccp_analysis_model:model` implements the callable contract used by
`agentcore.analysis_runner`: `model(messages, **options) -> str`. The default
CCP logical alias is `flash`; trusted callers may select `main` and set a
bounded `timeout_seconds`. The adapter uses the existing CCP credential
resolver and sends a single OpenAI-compatible chat-completions request to its
configured loopback `/v1` endpoint. It never sends tool/function definitions,
never accepts a model name or URL from the model input, and never puts a key in
process arguments or error text.

The endpoint is restricted to loopback HTTP, environment proxies are disabled,
redirects are rejected, the response is size-limited, and provider tool-call
responses or truncated output fail closed. The runner remains responsible for
validating JSON or a complete Markdown-fenced JSON report and source citations.

This is an opt-in adapter: no production policy or runtime configuration is
changed here. The model receives only the synthetic inputs prepared by
`tools/colleague_model_smoke.py` during the smoke check. Local tests use a fake
transport; the one real CCP flash smoke verifies a single synthetic analysis
path, not provider-wide isolation or reliability.
