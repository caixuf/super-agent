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


class DeliverableTransport:
    """Deliver sanitized text and upload attached/extracted artifacts directly to Feishu."""

    def __init__(self, completion_transport, feishu_transport):
        self.completion_transport = completion_transport
        self.feishu_transport = feishu_transport

    def deliver(self, address, message, *, idempotency_key):
        summary = message.get("summary", "")
        sanitized_summary, detected = sanitize_and_extract_artifacts(summary)

        # Merge explicitly declared artifacts with detected ones
        existing_artifacts = list(message.get("artifacts") or [])
        for p in existing_artifacts:
            ext = Path(p).suffix.lower()
            kind = "image" if ext in {".png", ".jpg", ".jpeg", ".webp"} else "file"
            if not any(d.get("path") == str(p) for d in detected):
                detected.append({"kind": kind, "path": str(p), "name": Path(p).name})

        # Deliver sanitized primary text receipt
        clean_msg = dict(message, summary=sanitized_summary, artifacts=[], artifact_paths=[])
        res = self.completion_transport.deliver(address, clean_msg, idempotency_key=idempotency_key)

        # Upload and deliver real artifacts as Feishu chat attachments
        if res and res.get("accepted") and self.feishu_transport:
            for idx, art in enumerate(detected):
                art_key = f"{idempotency_key}_art_{idx}"
                try:
                    p = Path(art["path"])
                    if not p.exists() or not p.is_file():
                        continue
                    if art["kind"] == "image":
                        img_key = self.feishu_transport.upload_image(str(p))
                        self.feishu_transport.send_image(origin=address, delivery_key=art_key, image_key=img_key)
                    else:
                        file_key = self.feishu_transport.upload_file(str(p))
                        self.feishu_transport.send_file(origin=address, delivery_key=art_key, file_key=file_key)
                except Exception:
                    # Best-effort artifact upload; never fails the core receipt delivery
                    pass
        return res


def build(root, *, profile, journal=None, env=None, opener=None, deliver_attachments=None):
    """Return ``(pool, completion_transport)``, or ``None`` without credentials."""
    root = Path(root)
    transport = from_env(journal or root / "feishu" / "sends.db", env, opener)
    if transport is None:
        return None
    completion = DirectCompletionTransport(
        root / "direct", profile=profile, sender=FeishuCompletionSender(transport),
        render=render, attempt_root=root / "attempts",
        attempt_provider=lambda key: None)
    
    use_attachments = deliver_attachments if deliver_attachments is not None else (
        os.environ.get("FEISHU_DELIVER_ATTACHMENTS", "0") == "1"
    )
    adapter = DeliverableTransport(completion, transport) if use_attachments else _TextOnly(completion)
    return Pool(str(root)), adapter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="queue root (pool.db parent)")
    parser.add_argument("--profile", required=True, help="profile that owns the origins")
    parser.add_argument("--journal", help="send journal (default <root>/feishu/sends.db)")
    parser.add_argument("--limit", type=int, default=5, help="receipts to try this run")
    parser.add_argument("--deliver-attachments", action="store_true", default=None,
                        help="upload and deliver real chat attachments for artifacts")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    built = build(args.root, profile=args.profile, journal=args.journal,
                  deliver_attachments=args.deliver_attachments)
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
