# T9 retrieval/fetch contract — 2026-10-26

## Status
DONE (new files only; no commit, no production writes, no network).

## Files
- `agentcore/learning/retrieval.py` (new)
- `tests/test_learning_retrieval.py` (new)

## API
- `FetchedSource(url, text, content_hash, retrieved_at, http_status=None, content_type=None)`
  - `from_text(...)` computes declared hash; `computed_hash`; `byte_size`; `hash_matches()`
- `Fetcher` — `runtime_checkable` Protocol: `fetch(url) -> FetchedSource`, injected only
- `ingest_fetched(store, fetched, *, statement, literal_quote, domain, subject=None, author=None, max_bytes=DEFAULT_MAX_BYTES=256 KiB) -> IngestResult`
- `fetch_and_ingest(store, fetcher, url, *, ...) -> IngestResult` — protocol checks only (fetch impl, return type, URL not silently replaced)
- `content_hash_of(data)`, `DEFAULT_MAX_BYTES`
- Errors: `RetrievalError(CandidateError)`, `FetchRejected`, `OversizedSource`; reuses `InvalidCandidate` / `QuoteNotGrounded` / `EvidenceMismatch`

## Enforced invariants
- quote must be an exact substring of the fetched text (no case/whitespace normalization)
- hash recomputed from UTF-8 bytes; declared `content_hash` (bare hex or `sha256:` prefix) must match
- body > `max_bytes` rejected before any write; empty body, non-2xx, non-textual `content_type` rejected
- source identity `sha256(url + content_hash)` dedupes snapshots; identical candidate -> `created=False`; same source + different quote shares one snapshot
- candidates can only be created `pending`; `CandidateStore.ingest` re-verifies hash and quote (defense in depth)
- no network/timer/memory-write imports; no `eval`/`exec`; fetcher is the sole I/O boundary

## Verification
```
cd agentcore
TMPDIR=/dev/shm python3 -m pytest tests/test_learning_retrieval.py -q
# 43 passed in 0.18s
TMPDIR=/dev/shm python3 -m pytest tests/test_learning_retrieval.py tests/test_learning_candidates.py -q
# 83 passed in 0.23s
```

## Remaining (deliberate)
- real fetcher implementation (HTTP, retries, rate limits, robots, redirect policy)
- review interaction / operator UI
- formal memory write (`Candidate.as_fact_proposal()` consumer / `write_fact`)
- post-write recall probe and cross-scope leakage checks
