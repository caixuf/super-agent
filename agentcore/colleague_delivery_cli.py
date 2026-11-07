"""Host-side drain of pending task completions through the real Feishu channel.

Nothing sends by itself: the host runs this after the queue holds terminal
receipts. It refuses to start without credentials, sends at most once per
delivery key, and reports ``delivery_unknown`` rather than retrying an
ambiguous send. Attachment delivery is opt-in and uses the durable transport's
owning-attempt validation, snapshots and per-operation journal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agentcore.completion_delivery import deliver_pending
from agentcore.delivery_sanitizer import sanitize_and_extract_artifacts
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
        text += "\n附件（未上传）:\n" + "\n".join(str(f) for f in files)
    return text[:MAX_TEXT]

#: Statuses that mean the host must look, not that the drain failed to run.
_ATTENTION = {"delivery_unknown", "ack_unknown", "sent_ack_rejected", "delivery_rejected"}


class _TextOnly:
    """Attachments are described in the body; upload is not implemented here.

    ``DirectCompletionTransport`` stages every path in ``artifacts``. When uploads
    are disabled, display file names without exposing private host directories.
    """

    def __init__(self, transport):
        self.transport = transport

    def deliver(self, address, message, *, idempotency_key):
        summary, _ = sanitize_and_extract_artifacts(message.get('summary', ''))
        names = [Path(str(path)).name for path in message.get('artifacts') or []]
        body = dict(message, summary=summary, artifacts=[], artifact_paths=names)
        return self.transport.deliver(address, body, idempotency_key=idempotency_key)


class DeliverableTransport:
    """Use the durable transport for both text and attempt-owned attachments."""

    def __init__(self, completion_transport, feishu_transport):
        self.completion_transport = completion_transport
        self.feishu_transport = feishu_transport

    def deliver(self, address, message, *, idempotency_key):
        summary = message.get("summary", "")
        attempt = self.completion_transport.attempt_provider(idempotency_key)
        allowed_dir = None
        if (isinstance(attempt, str) and len(attempt) == 32
                and all(c in '0123456789abcdef' for c in attempt)):
            allowed_dir = self.completion_transport.attempt_root / attempt
        sanitized_summary, detected = sanitize_and_extract_artifacts(
            summary, base_dir=allowed_dir, allow_nonexistent=True)

        # Merge explicitly declared artifacts with detected ones
        files = list(message.get("artifacts") or [])
        for art in detected:
            if art['path'] not in files and len(files) < 8:
                files.append(art['path'])
        clean_msg = dict(message, summary=sanitized_summary, artifacts=files, artifact_paths=[])
        return self.completion_transport.deliver(address, clean_msg, idempotency_key=idempotency_key)


def build(root, *, profile, journal=None, env=None, opener=None,
          deliver_attachments=None, deliver_card=None):
    """Return ``(pool, completion_transport)``, or ``None`` without credentials."""
    root = Path(root)
    transport = from_env(journal or root / "feishu" / "sends.db", env, opener)
    if transport is None:
        return None
    pool = Pool(str(root))
    source_env = os.environ if env is None else env
    use_card = deliver_card if deliver_card is not None else (
        source_env.get("FEISHU_DELIVER_CARD", "0") == "1"
    )
    completion = DirectCompletionTransport(
        root / "direct", profile=profile, sender=FeishuCompletionSender(transport),
        render=render, attempt_root=root / "attempts",
        attempt_provider=lambda key: pool.status(key).get('attempt'),
        card=use_card)
    
    use_attachments = deliver_attachments if deliver_attachments is not None else (
        source_env.get("FEISHU_DELIVER_ATTACHMENTS", "0") == "1"
    )
    adapter = DeliverableTransport(completion, transport) if use_attachments else _TextOnly(completion)
    return pool, adapter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="queue root (pool.db parent)")
    parser.add_argument("--profile", required=True, help="profile that owns the origins")
    parser.add_argument("--journal", help="send journal (default <root>/feishu/sends.db)")
    parser.add_argument("--limit", type=int, default=5, help="receipts to try this run")
    parser.add_argument("--deliver-attachments", action="store_true", default=None,
                        help="upload and deliver real chat attachments for artifacts")
    parser.add_argument("--deliver-card", action="store_true", default=None,
                        help="deliver completions using canonical CardKit 2.0 interactive cards")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    built = build(args.root, profile=args.profile, journal=args.journal,
                  deliver_attachments=args.deliver_attachments,
                  deliver_card=args.deliver_card)
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
