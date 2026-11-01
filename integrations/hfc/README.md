# HFC Integration

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
