# T9 bounded HTTP fetcher — 2026-10-26

## Status
DONE (new files only; no commit, no production writes, no external network).

## Files
- `agentcore/learning/http_fetcher.py` (new)
- `tests/test_learning_http_fetcher.py` (new)
- `docs/reports/2026-10-26_t9_http_fetcher.md` (this report, untracked)

No existing file was modified; `retrieval.py`'s `Fetcher` protocol / `FetchedSource` /
`FetchRejected` / `OversizedSource` / `RetrievalError` are reused as-is.

## API

```python
HttpFetcher(*, allowed_hosts, timeout=5.0, max_bytes=262144,
            max_redirects=2, user_agent="agentcore-http-fetcher/1.0")
    .fetch(url) -> FetchedSource
```

- `allowed_hosts`: iterable of bare hostnames. Exact match, or subdomain suffix match
  (`example.test` -> `api.example.test`, but not `badexample.test`). IP literals match
  exactly only (`1.2.3.4` is not a subdomain of `2.3.4`). Ports are not part of the
  allowlist (the local test server uses an ephemeral loopback port); each redirect hop is
  re-checked against the same rules. Empty set / single string / entry with scheme,
  port, path, credential, whitespace -> `ValueError` (configuration error, default deny).
- `timeout`: finite positive seconds, per socket operation (connect + read).
- `max_bytes`: positive int, hard cap on response body (default 262144 = 256 KiB).
- `max_redirects`: non-negative int, `0` refuses every redirect.
- `user_agent`: non-empty single-line string, sent on the request.
- Instances keep no per-request state (redirect counter lives in the per-call opener), so
  one fetcher can be shared across threads.

### Enforcement (in order)
1. URL must be ASCII, absolute `http`/`https`, no whitespace/control chars, no backslash;
   no embedded username/password; valid port; missing host rejected.
2. Initial host must pass the allowlist **before any socket work**.
3. Single GET, headers `User-Agent`, `Accept: text/*, application/json;q=0.9`,
   `Accept-Encoding: identity`, `Connection: close`. No retries.
4. Redirects: each hop re-validated by `_BoundedRedirectHandler` (allowlist, http(s),
   no credentials); more than `max_redirects` -> `FetchRejected`; https -> http downgrade
   -> `FetchRejected`.
5. Final status must be 2xx; 3xx/4xx/5xx -> `FetchRejected`.
6. `Content-Type` required, media type must be `text/*` or `application/json`
   (parameters stripped); missing / other -> `FetchRejected`.
7. `Content-Encoding` must be absent/`identity`; gzip/br/etc -> `FetchRejected`
   (the body is never decompressed, so compressed bytes cannot become evidence).
8. `Content-Length` over `max_bytes` -> `OversizedSource` before reading; the guarded
   `read(max_bytes + 1)` catches lying/absent headers -> `OversizedSource`.
9. Body decoded with the declared charset (strict), else UTF-8 (BOM tolerant) strict;
   unknown charset / undecodable / truncated read / timeout -> `RetrievalError`.
10. Blank body -> `FetchRejected`.
11. Result: `FetchedSource.from_text(url=<requested URL>, ..., http_status,
    content_type)`; `content_hash` is recomputed from the decoded text and `retrieved_at`
    is UTC ISO-8601 `...Z`. `url` stays the **requested** URL even across redirects,
    satisfying `fetch_and_ingest`'s source-identity check.

### Deliberately absent
- **robots.txt**: not parsed or honored.
- **JS / HTML rendering**: not executed or parsed; body is returned as text.
- **Retries / rate limiting / backoff**: none; one attempt per `fetch`, caller owns retry.
- Cookies, auth headers, proxies, DNS pinning, TLS client certs / verify-off switch:
  none. HTTPS uses stdlib default certificate verification (system CA).
- No persistence, no `CandidateStore` access, no `write_fact`.

## Verification
```
ssh 远端宿主
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_learning_http_fetcher.py -q
# 36 passed in 1.67s
TMPDIR=/dev/shm python3 -m pytest tests/test_learning_http_fetcher.py \
    tests/test_learning_retrieval.py tests/test_learning_candidates.py -q
# 119 passed in 1.56s
python3 -m py_compile agentcore/learning/http_fetcher.py \
    tests/test_learning_http_fetcher.py
# COMPILE_OK
```

All traffic in the test module is a `ThreadingHTTPServer` bound to `127.0.0.1:0` in a
daemon thread; disallowed hosts (`evil.invalid`, `allowed.invalid`) are rejected by policy
before any connection/DNS attempt. Covered cases (36 tests):
success + `fetch_and_ingest` quoted ingest (pending candidate, snapshot, recomputed hash);
`application/json` accepted; 302 -> allowed path followed, `FetchedSource.url` unchanged;
404 rejected; `image/png` and missing `Content-Type` rejected; empty body rejected;
`Content-Encoding: gzip` rejected; body over `max_bytes` rejected; oversized declared
`Content-Length` rejected before reading; redirect to disallowed host rejected;
redirect loop over `max_redirects` rejected (and `max_redirects=0`); connect-time and
read-time timeout -> `RetrievalError` (not `FetchRejected`); host outside allowlist
rejected before network; embedded credentials rejected; `file`/`ftp`/scheme-relative/
host-less/bad-port URLs rejected; allowlist exact/subdomain/IP semantics; constructor
validation for hosts/timeout/max_bytes/max_redirects/user_agent; `repr` has no secrets.

Note: `CandidateError` subclasses `ValueError`, so `fetch()` re-raises `RetrievalError`
before its generic `ValueError` fallback; the redirect-policy tests pin that ordering.

## Remaining (deliberate)
- robots.txt handling and per-host politeness/rate limiting are not implemented (host
  policy must be enforced by the allowlist and caller scheduling).
- retries/backoff, and any real TLS client-cert/mTLS or custom-CA policy for production
  targets are out of scope; the fetcher uses stdlib default HTTPS verification.
- no proxy support, no DNS pinning/rebinding defense beyond the host allowlist, no IDNA
  (non-ASCII URLs are rejected).
- HTML->text extraction, JS rendering, and content dedup/redirect-canonicalization are
  not part of this layer.
- no caller in the repository constructs `HttpFetcher` yet; wiring it into a retrieval
  job/CLI (default off) remains a follow-up.
