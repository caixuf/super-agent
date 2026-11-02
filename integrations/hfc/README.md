# HFC Integration

## Delivery safety hold — 2026-11-02

**The pinned v5.0.13 is not eligible for production interaction acceptance.**
Isolated fault injection reproduced premature terminal acknowledgement, definite
terminal delivery failure reported as successful native delivery, and suppression
of an unseen late answer after display supersession. See
[`delivery investigation`](../../docs/reports/2026-11-02-hfc-delivery-loss.md).
Do not newly enable it on a live profile merely because doctor is healthy.
Existing deployments need a separately authorized, idle-window rollback through
HFC’s CLI; do not edit installed Hermes targets or disable the sidecar alone.

`lock.json.known_delivery_blockers` records the release hold. The default checker
fails `no_known_delivery_loss` even when started/answer/completed samples exist.
`--infrastructure-only` can still report infrastructure ready, but always reports
`interaction_gate_passed: false`; it does not clear the hold. Even a future
passing automated gate cannot prove the manual client criteria below. An absent safety review
also blocks the interaction gate. Remove blocker IDs only after a reviewed fix
and regression evidence on the exact candidate revision, not to make a gate green.

Opt-in upstream safety regression (private in-memory platform + loopback HTTP;
no real bot messages or installed-source edits):

```bash
HFC_SOURCE_ROOT=/trusted/hfc/source python3 -m pytest -q tests/test_hfc_delivery_contract.py
```

The current pin produces three failing safety assertions and two passing controls.
Without an external source the repository suite explicitly skips these tests;
that skip is not HFC delivery acceptance. Use a test interpreter with HFC runtime
dependencies. Sources/tests must match the pinned Git blobs.

## Version alignment vs delivery correctness

Hermes stable and HFC stable were checked on 2026-11-02: Hermes v0.22.5
(`bf0203a794a07aac3ff764972ab9087baa4e4f4e`) is the latest release and HFC
v5.0.13 (`e072763f1b6e3170a24be70fc7474df05ee0552f`) is its latest release. The
local Hermes install is already that stable tag, so “pull both newest releases”
is not a remaining upgrade. The current local runtime instead carries a heavily
modified Feishu adapter and multiple local delivery/plugin changes.

Keep two gates separate: (1) version/provenance alignment across Hermes, HFC,
plugins, profile config and installed manifest; (2) delivery-correctness fault
injection. A version match cannot clear a known fake-success/lost-answer path,
and a passing delivery test cannot excuse an unpinned/drifted install.

A candidate repair and its evidence are recorded in
[`../../patches/hfc-delivery-safety/README.md`](../../patches/hfc-delivery-safety/README.md).
It is **not deployed**, does not clear this release hold by itself, and must go
through HFC's normal installer against an exact candidate revision.

## Ownership

HFC (`hermes-feishu-streaming-card`) owns Feishu streaming-card delivery through
its upstream CLI, Hermes hooks, runtime dependency and sidecar. This repository
does not vendor HFC code or take ownership of those files.

`lock.json` pins the evaluated release and source commit. The checker also
reads the CLI environment's installed distribution `direct_url.json` through
the CLI's own Python interpreter, so matching a version string alone is not
enough. This does not independently prove the Hermes PM runtime wheel's source
revision. Install, upgrade,
repair and rollback through HFC's own CLI using that exact source revision;
never copy a wheel, hand-edit HFC-managed Hermes files, or run setup against a
production HOME during isolated tests.

## Read-Only Check

After installation, run the bounded infrastructure check with explicit paths:

```bash
python3 tools/hfc_acceptance.py \
  --doctor-cli /path/to/hermes-feishu-card \
  --config /path/to/hfc.yaml \
  --hermes-dir /path/to/hermes-agent \
  --hermes-home /path/to/hermes-home
```

The checker invokes only `hermes-feishu-card doctor --support-report`. It does
not install, repair, start, stop, restart, upload reports or send Feishu
messages. Its JSON output contains check identifiers and states, not the source
report, paths, credentials, endpoints or chat contents.

The default gate also requires retained evidence of a started turn, first
answer and completed turn. Use `--infrastructure-only` only to distinguish a
healthy installation from missing interaction evidence; it is not production
acceptance.

## Client Acceptance

The report cannot prove what a Feishu client rendered or whether a user action
resumed the same Gateway turn. On a private test bot/chat, manually verify:

- one card per turn, with non-empty first content and final content retained;
- stable in-place updates across tools, one terminal state and no duplicate text;
- button/clarification callback resumes the same turn;
- reconnect preserves bot and route.

Do not enable HFC beside another card producer until their ownership is
explicitly separated. In particular, review native `card_replies`, task-progress
card delivery and other plugin notifications before a production cutover.
