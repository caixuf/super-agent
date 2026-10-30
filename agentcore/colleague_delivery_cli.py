"""Host-side drain of pending task completions through the real Feishu channel.

Nothing sends by itself: the host runs this after the queue holds terminal
receipts. It refuses to start without credentials, sends at most once per
delivery key, and reports ``delivery_unknown`` rather than retrying an
ambiguous send. Attachments are not implemented here: a receipt with artifacts
raises and stays pending.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentcore.completion_delivery import deliver_pending
from agentcore.direct_completion import DirectCompletionTransport
from agentcore.feishu_transport import FeishuCompletionSender, FeishuTransport, from_env
from agentcore.pool import Pool

#: Longest body one plain-text completion carries; longer reports stay on disk.
MAX_TEXT = 30000


def render(message):
    """Plain-text body for one completion, including where its files are."""
    label = message.get("colleague_label")
    head = "【%s】" % label if isinstance(label, str) and label else ""
    text = "%s任务 %s %s\n%s" % (head, message.get("task_id"), message.get("status"),
                                 message.get("summary", ""))
    if message.get("truncated"):
        text += "\n（已截断，完整报告见任务结果）"
    files = message.get("artifact_paths") or []
    if files:
        text += "\n附件（未上传，路径）:\n" + "\n".join(str(f) for f in files)
    return text[:MAX_TEXT]

#: Statuses that mean the host must look, not that the drain failed to run.
_ATTENTION = {"delivery_unknown", "ack_unknown", "sent_ack_rejected", "delivery_rejected"}


class _TextOnly:
    """Attachments are described in the body; upload is not implemented here.

    ``DirectCompletionTransport`` stages every path in ``artifacts`` and would fail
    on the first one forever. Moving them to a descriptive field keeps the receipt
    deliverable and says plainly where the files are.
    """

    def __init__(self, transport):
        self.transport = transport

    def deliver(self, address, message, *, idempotency_key):
        body = dict(message, artifacts=[], artifact_paths=list(message.get("artifacts") or []))
        return self.transport.deliver(address, body, idempotency_key=idempotency_key)


def build(root, *, profile, journal=None, env=None, opener=None):
    """Return ``(pool, completion_transport)``, or ``None`` without credentials."""
    root = Path(root)
    transport = from_env(journal or root / "feishu" / "sends.db", env, opener)
    if transport is None:
        return None
    completion = DirectCompletionTransport(
        root / "direct", profile=profile, sender=FeishuCompletionSender(transport),
        render=render, attempt_root=root / "attempts",
        attempt_provider=lambda key: None)
    return Pool(str(root)), _TextOnly(completion)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="queue root (pool.db parent)")
    parser.add_argument("--profile", required=True, help="profile that owns the origins")
    parser.add_argument("--journal", help="send journal (default <root>/feishu/sends.db)")
    parser.add_argument("--limit", type=int, default=5, help="receipts to try this run")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    built = build(args.root, profile=args.profile, journal=args.journal)
    if built is None:
        print(json.dumps({"ok": False, "error": "feishu_credentials_missing"}))
        return 2
    pool, completion = built
    outcomes = deliver_pending(pool, completion, limit=args.limit)
    print(json.dumps({"ok": not any(o.get("status") in _ATTENTION for o in outcomes),
                      "outcomes": outcomes}, ensure_ascii=False))
    return 0 if not any(o.get("status") in _ATTENTION for o in outcomes) else 3


if __name__ == "__main__":
    raise SystemExit(main())
