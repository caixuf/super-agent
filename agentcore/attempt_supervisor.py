"""Linux process-tree supervisor for administrator-supplied local runners.

This is lifecycle supervision, not a hostile-code sandbox. The runner must not
attack the supervisor, modify its private files, or delegate work to external
daemons. Unlike an environment scan, subreaping still covers double forks,
setsid(), and environment clearing within the supervised process tree.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys


def process_start(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + fields[19]
    except (OSError, IndexError):
        return None


def _enable_subreaper():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("linux_subreaper_required")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "subreaper_unavailable")
    value = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(value), 0, 0, 0) != 0 or value.value != 1:
        raise RuntimeError("subreaper_not_enabled")


def _kill_children():
    # This supervisor is single-threaded. Children cannot reuse their PIDs until
    # we reap them, so signalling our own still-unreaped children is PID safe.
    for name in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split():
        try:
            os.kill(int(name), signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    evidence_fd, lock_fd = int(sys.argv[2]), int(sys.argv[3])
    config = json.loads(control.recv(1 << 20))
    stopped = False

    def request_stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    proc, code, ready = None, 125, False
    child_started = False
    try:
        _enable_subreaper()  # No child may exist before this succeeds.
        ready = True
        limit = config["max_file"]
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid_file_limit")
    except Exception:
        stopped = True

    try:
        # Do not publish a signalable PID before our handlers are installed.
        control.sendall(b"ready")
        authorized = control.recv(16) == b"go"
        if authorized and not stopped:
            try:
                command = [sys.executable, str(Path(__file__).with_name("exec_runner.py")),
                           str(limit)] + config["command"]
                env = dict(os.environ)
                if config.get("token"):
                    env["AGENTCORE_ATTEMPT_TOKEN"] = config["token"]
                # The runner inherits neither control/evidence descriptors nor
                # the lock. The supervisor retains it until its tree is reaped.
                proc = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout,
                                        stderr=sys.stderr, start_new_session=True, close_fds=True,
                                        env=env)
                child_started = True
            except Exception:
                stopped = True
        else:
            stopped = True
            code = -signal.SIGTERM
        while True:
            no_children = False
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    no_children = True
                    break
                except InterruptedError:
                    continue
                if pid == 0:
                    break
                if proc is not None and pid == proc.pid:
                    code = os.waitstatus_to_exitcode(status)
                    proc.returncode = code
                    stopped = True
            if no_children:
                break
            readable, _, _ = select.select([control], [], [], .02)
            if readable:
                # Any control message or EOF is a stop request. A dead Worker
                # cannot leave an executing child orphaned behind a stale lease.
                control.recv(1024)
                stopped = True
            if stopped:
                _kill_children()
        # ECHILD after subreaping is the proof, not main-PID exit or token emptiness.
        proof = {name: config[name] for name in ("job", "attempt", "token", "nonce")}
        proof.update(quiescent=True, proof_scope="subreaper_tree" if ready else "not_started",
                     supervisor_pid=os.getpid(), supervisor_start=process_start(os.getpid()),
                     child_started=child_started, exit_code=code)
        blob = json.dumps(proof, sort_keys=True).encode()
        written = 0
        while written < len(blob):
            written += os.write(evidence_fd, blob[written:])
        os.fsync(evidence_fd)
    finally:
        # An unexpected observation/IO failure emits no valid proof. Never
        # convert an uncertain stop into a terminal result in the Worker.
        control.close()
        os.close(evidence_fd)
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
