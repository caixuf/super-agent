"""Operator CLI for the capability-drift guard: patrol result -> durable alert.

The host's authoritative inspector stays in charge of detection. This CLI
consumes an already-computed patrol result as JSON, records it in the durable
alert outbox, and optionally delivers one message through the HTTP webhook
sender.

Safety defaults:

* dry-run is the default. It parses and renders, but never creates the outbox,
  never records an episode, and never contacts a webhook.
* ``--no-dry-run`` performs the real guarded write/send.
* the token is never printed or stored in a summary.

Exit codes:

==  =====================================================================
0   quiet, deduplicated, or delivered; also a valid dry-run preview
2   malformed input: unreadable/invalid JSON, invalid patrol shape,
    invalid webhook configuration, or live options contradicting the patrol
3   no delivery target configured, or the alert queue is unavailable
4   delivery failed or its outcome is unknown; the event stays pending
==  =====================================================================

Deliberate gaps (host-owned):

* no inspector invocation: the caller serializes
  ``ops_patrol.inspect_capability_baseline`` itself and passes the result;
* no target policy: this CLI reads no environment variables and no host
  configuration, so a URL/token must be supplied explicitly or nothing is sent.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Sequence

from agentcore.capability_guard import format_alert, inspection_from_patrol, run_guard
from agentcore.ops_alert_senders import WebhookSender
from agentcore.ops_alerts import AlertOutbox

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_NO_TARGET = 3
EXIT_DELIVERY = 4


class InputError(ValueError):
    """Malformed patrol input, invalid configuration, or conflicting options."""


def load_patrol(path: str, *, stdin: Any = None) -> dict[str, Any]:
    """Read one patrol result from a file, or stdin when ``path`` is ``-``."""
    origin = "<stdin>" if path == "-" else path
    try:
        if path == "-":
            raw = (stdin or sys.stdin).read()
        else:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read()
    except OSError as exc:
        raise InputError(
            "cannot read patrol JSON from %s: %s" % (origin, exc.strerror or exc)
        ) from exc
    try:
        patrol = json.loads(raw)
    except ValueError as exc:
        raise InputError("patrol JSON from %s is not valid JSON: %s" % (origin, exc)) from exc
    if not isinstance(patrol, dict):
        raise InputError("patrol JSON from %s must be an object" % origin)
    return patrol


def preview(patrol: dict[str, Any], *, profile: str, scope: str,
            config_persisted: bool | None = None,
            runtime_loaded: bool | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive the inspection and the event-shaped payload for one observation.

    The patrol remains the source of truth. The optional overrides only refine
    the rendered preview in dry-run mode; live mode refuses contradictions so
    the durable event never records a fact the inspector did not report.
    """
    try:
        inspection = inspection_from_patrol(patrol)
    except ValueError as exc:
        raise InputError("invalid patrol result: %s" % exc) from exc
    event = {
        "kind": "capability_drift",
        "profile": profile,
        "scope": scope,
        "expected": inspection["expected"],
        "observed": inspection["observed"],
        "config_persisted": inspection["config_persisted"],
        "runtime_loaded": inspection["runtime_loaded"],
    }
    if config_persisted is not None:
        event["config_persisted"] = bool(config_persisted)
    if runtime_loaded is not None:
        event["runtime_loaded"] = bool(runtime_loaded)
    return inspection, event


def _reject_live_contradictions(inspection: dict[str, Any], *,
                                config_persisted: bool | None,
                                runtime_loaded: bool | None) -> None:
    """Refuse live overrides that would falsify the recorded event."""
    conflicts = []
    for name, requested, derived in (
        ("--config-persisted", config_persisted, inspection["config_persisted"]),
        ("--runtime-loaded", runtime_loaded, inspection["runtime_loaded"]),
    ):
        if requested is not None and bool(requested) != bool(derived):
            conflicts.append(
                "%s=%s contradicts the patrol (%s)" % (name, bool(requested), bool(derived))
            )
    if conflicts:
        raise InputError(
            "; ".join(conflicts) + "; the durable event must reflect the inspector"
        )


def _build_sender(args: argparse.Namespace) -> WebhookSender | None:
    """Validate and build the transport, or return None when unconfigured."""
    if not args.webhook_url:
        return None
    try:
        return WebhookSender(args.webhook_url, token=args.token, timeout=args.timeout)
    except ValueError as exc:
        raise InputError("invalid webhook configuration: %s" % exc) from exc


def _emit(summary: dict[str, Any]) -> int:
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return int(summary["exit_code"])


def _fail(kind: str, detail: str, code: int) -> int:
    print(json.dumps({"status": "error", "error": kind, "detail": detail,
                      "exit_code": code}, ensure_ascii=False, indent=2),
          file=sys.stderr)
    return code


def _dry_run_summary(inspection: dict[str, Any], event: dict[str, Any],
                     *, sender_configured: bool) -> dict[str, Any]:
    drifted = bool(inspection["drifted"])
    return {
        "dry_run": True,
        "status": "preview" if drifted else "quiet",
        "drifted": drifted,
        "message": format_alert(event) if drifted else None,
        "config_persisted": event["config_persisted"],
        "runtime_loaded": event["runtime_loaded"],
        "webhook_configured": sender_configured,
        "would_deliver": drifted and sender_configured,
        "exit_code": EXIT_OK,
    }


def _exit_for(result: dict[str, Any], *, sender_configured: bool) -> int:
    status = result["status"]
    if status in ("quiet", "delivered", "deduplicated"):
        return EXIT_OK
    # Every remaining status means the outbox still holds an undelivered event.
    return EXIT_DELIVERY if sender_configured else EXIT_NO_TARGET


def _run_live(args: argparse.Namespace, patrol: dict[str, Any],
              sender: WebhookSender | None) -> int:
    if not args.queue_root:
        raise InputError("--queue-root is required unless --dry-run is set")
    try:
        outbox = AlertOutbox(args.queue_root)
        result = run_guard(outbox=outbox, patrol=patrol, profile=args.profile,
                           scope=args.scope, sender=sender)
    except (OSError, sqlite3.Error) as exc:
        return _fail("queue_unavailable", str(exc), EXIT_NO_TARGET)
    except ValueError as exc:
        raise InputError("guard rejected the patrol result: %s" % exc) from exc

    code = _exit_for(result, sender_configured=sender is not None)
    episode = result.get("episode") or {}
    summary = {
        "dry_run": False,
        "status": result["status"],
        "drifted": bool(episode.get("drifted")),
        "message": result.get("message"),
        "webhook_configured": sender is not None,
        "event_id": episode.get("event_id"),
        "delivery": result.get("delivery"),
        "pending_count": result.get("pending_count"),
        "exit_code": code,
    }
    return _emit(summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capability_guard_cli",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument("--patrol-json", required=True, metavar="FILE|-",
                        help="patrol result JSON file, or - to read stdin")
    parser.add_argument("--queue-root", default=None,
                        help="alert outbox root; required unless --dry-run")
    parser.add_argument("--profile", required=True,
                        help="host profile key for episode deduplication")
    parser.add_argument("--scope", required=True,
                        help="host scope key for episode deduplication")
    parser.add_argument("--webhook-url", default=None,
                        help="absolute http(s) endpoint; omit to record without sending")
    parser.add_argument("--token", default=None,
                        help="bearer token for the webhook (never printed)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="webhook timeout in seconds (default: 5.0)")
    parser.add_argument("--config-persisted", default=None,
                        action=argparse.BooleanOptionalAction,
                        help="preview-only override; live mode refuses contradictions")
    parser.add_argument("--runtime-loaded", default=None,
                        action=argparse.BooleanOptionalAction,
                        help="preview-only override; live mode refuses contradictions")
    parser.add_argument("--dry-run", default=True,
                        action=argparse.BooleanOptionalAction,
                        help="parse and render only (default); --no-dry-run sends")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        patrol = load_patrol(args.patrol_json)
        inspection, event = preview(
            patrol, profile=args.profile, scope=args.scope,
            config_persisted=args.config_persisted,
            runtime_loaded=args.runtime_loaded,
        )
        sender = _build_sender(args)
        if args.dry_run:
            summary = _dry_run_summary(inspection, event,
                                       sender_configured=sender is not None)
            return _emit(summary)
        _reject_live_contradictions(
            inspection,
            config_persisted=args.config_persisted,
            runtime_loaded=args.runtime_loaded,
        )
        if args.token and not args.webhook_url:
            print("warning: --token supplied without --webhook-url; "
                  "the alert stays pending", file=sys.stderr)
        return _run_live(args, patrol, sender)
    except InputError as exc:
        return _fail("input", str(exc), EXIT_INPUT)


if __name__ == "__main__":
    raise SystemExit(main())
