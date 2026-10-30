# Read-only failure analysis runner

Date: 2026-10-26. Implementation and isolated verification; production acceptance remains pending.

## Receipt

- Code: `agentcore/analysis_runner.py`, `tests/test_analysis_runner.py`.
- Result: bounded read-only snapshots plus injected model analysis and deterministic citation validation.
- Verification: `python3 -m pytest tests/test_analysis_runner.py tests/test_repo_neutrality.py -q` → **32 passed** using a private local tmpfs test directory.
- Production: no model credentials read, no remote model call, no service operation, no outbound message, no commit or push by this slice.

## Interface

```python
run_analysis(job, policy, model, output_dir='.') -> receipt
```

`job` has `job`, `attempt`, and `payload={goal,sources:[{path,start_line?,end_line?}]}`.
All source paths are absolute. The admin policy requires `allowed_roots` and can
set `denied_globs`, `max_files` (8), `max_file_bytes` (262144), `max_total_bytes`
(1048576), and `max_lines` (400 per source). Source line ranges are one-based,
inclusive. Files larger than their byte limit are rejected rather than truncated.

CLI:

```text
python3 -m agentcore.analysis_runner --policy /absolute/policy.json \
  --model-adapter approved_module:callable --output-dir .
```

The administrator selects the importable adapter, never the model/task payload.
The adapter receives `messages` and returns JSON text, a `content` envelope, or a
report mapping. It should enforce its own model request timeout and token budget;
the outer durable worker enforces process wall-clock and file/output budgets.
The runner gives the model no shell or filesystem tools and no chat transcript.

Required report shape:

```json
{
  "conclusion": "Observation and uncertainty",
  "claims": [{
    "statement": "A supported observation",
    "evidence": [{"source_id": "source_1", "line": 2, "quote": "exact text"}]
  }],
  "limitations": ["Unresolved questions"]
}
```

## Verification boundary

Each citation must name an included snapshot and a supplied line, and quote a
nonempty verbatim substring of that line. Unsupported references, out-of-range
lines, invented quotes, empty claims/evidence and malformed reports fail.

Success means **citation_traceability_only**. It does not prove that a model's
causal interpretation follows from its quotes. Independent review or a targeted
test is still necessary before claiming a root cause or a successful repair.

## Source and artifact handling

- Open directories and final files with anchored `openat` plus `O_NOFOLLOW`;
  reject symlinks, traversal, out-of-root paths and nonregular files.
- Reject credential-like filenames/directories, configured deny globs, private
  key blocks and common secret assignments before calling a model. This check
  can conservatively reject benign source containing secret-like examples and
  cannot establish that arbitrary logs are secret-free; provide approved inputs.
- Snapshot whole-file SHA-256, byte size, selected line numbers and text. Detect
  ordinary size/mtime changes during reading; this is not an immutable filesystem.
- Write `report.json`, `report.md`, then `manifest.json` using atomic replace and
  file/directory fsync. Use a fresh per-attempt output directory; the group of
  files is not one filesystem transaction and should not be reused concurrently.
- Input/model/config exceptions become fixed public error codes. Model adapter
  stdout is redirected to private stderr; CLI stdout contains one JSON receipt.
- No automatic delivery occurs. A host consumer selects the appropriate report
  fields and verifies scope before sending or exposing artifacts.

## Remaining acceptance

Host adapter integration, an approved real failure sample, actual model quality,
independent semantic review, persistent-worker end-to-end recovery and platform
delivery require separate acceptance. No production completion is claimed here.
