#!/usr/bin/env python3
"""Install the fail-closed card update guard on the pinned Hermes transport."""
import argparse
from pathlib import Path
import os
import tempfile

FIRST = '''        """First send, threaded to the user's message (correct topic/thread)."""
'''
GUARD = '''        if getattr(self, "_sa_card_update_unresolved", False):
            logger.warning("Card update outcome unknown; suppressing new-message fallback")
            return False
'''
SECOND = '''        from gateway.relay.egress import declined_send

'''
CLASSIFY = '''        if getattr(result, "error_kind", None) == "sa_card_update_unresolved":
            # A remote update may already have landed; do not create a second message.
            self._sa_card_update_unresolved = True
            self._edit_supported = False
            return False
'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-dir", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    path = args.agent_dir / "gateway/stream_consumer_transport.py"
    original = path.read_text(encoding="utf-8")
    if original.count(FIRST + GUARD) == 1 and original.count(SECOND + CLASSIFY) == 1:
        print("card delivery guard present"); return 0
    if args.check:
        print("card delivery guard absent or incompatible"); return 1
    if (original.count(FIRST) != 1 or original.count(SECOND) != 1 or
            "_sa_card_update_unresolved" in original):
        raise ValueError("card delivery guard incompatible")
    updated = original.replace(FIRST, FIRST + GUARD, 1).replace(SECOND, SECOND + CLASSIFY, 1)
    fd, tmp = tempfile.mkstemp(prefix=".card-delivery-guard-", dir=path.parent)
    try:
        os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(updated); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    print("card delivery guard installed"); return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print("card delivery guard failed: " + str(exc))
        raise SystemExit(1)
