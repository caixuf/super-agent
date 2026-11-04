import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "patches/apply_card_delivery_guard.py"
BASE = '''class Stream:
    async def _first_send(self, text):
        """First send, threaded to the user's message (correct topic/thread)."""
        if getattr(self, "_egress_declined", False): return False
    async def _handle(self, result):
        from gateway.relay.egress import declined_send

        if declined_send(result): return False
'''

def test_guard_applies_idempotently_and_rejects_partial_patch(tmp_path):
    path = tmp_path / "gateway/stream_consumer_transport.py"
    path.parent.mkdir()
    path.write_text(BASE)
    command = [sys.executable, str(SCRIPT), "--agent-dir", str(tmp_path)]
    assert subprocess.run(command + ["--check"], capture_output=True).returncode != 0
    assert subprocess.run(command + ["--apply"], capture_output=True).returncode == 0
    content = path.read_text()
    assert subprocess.run(command + ["--check"], capture_output=True).returncode == 0
    assert subprocess.run(command + ["--apply"], capture_output=True).returncode == 0
    assert path.read_text() == content
    import asyncio
    from types import SimpleNamespace
    namespace = {"logger": SimpleNamespace(warning=lambda *args: None)}
    exec(compile(content, str(path), "exec"), namespace)
    stream = namespace["Stream"]()
    stream._sa_card_update_unresolved = True
    assert asyncio.run(stream._first_send("message")) is False
    path.write_text(BASE + "\n# _sa_card_update_unresolved\n")
    before = path.read_bytes()
    assert subprocess.run(command + ["--apply"], capture_output=True).returncode != 0
    assert path.read_bytes() == before
