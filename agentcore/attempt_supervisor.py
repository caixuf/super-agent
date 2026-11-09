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
import time


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


def _kill_children() -> list[int]:
    # This supervisor is single-threaded. Children cannot reuse their PIDs until
    # we reap them, so signalling our own still-unreaped children is PID safe.
    # Returns list of PIDs that could not be killed (e.g. PermissionError on sudo children).
    unquiesced = []
    try:
        children_path = Path(f"/proc/self/task/{os.getpid()}/children")
        if not children_path.exists():
            return unquiesced
        pids = children_path.read_text().split()
    except (OSError, ProcessLookupError):
        return unquiesced
    for name in pids:
        try:
            pid = int(name)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Different UID / sudo child cannot be SIGKILLed directly; track it
            unquiesced.append(int(name))
        except OSError:
            pass
    return unquiesced


# Deterministic lifecycle states for attempt supervisor
STATE_RUNNING = "RUNNING"
STATE_STOPPING = "STOPPING"
STATE_EVIDENCE_EMITTED = "EVIDENCE_EMITTED"
REAPING_DEADLINE_SECONDS = 5.0


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    evidence_fd, lock_fd = int(sys.argv[2]), int(sys.argv[3])
    config = json.loads(control.recv(1 << 20))
    state = STATE_RUNNING
    stop_deadline = None
    unquiesced_pids = []

    def request_stop(*_):
        nonlocal state, stop_deadline
        if state == STATE_RUNNING:
            state = STATE_STOPPING
            stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS

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
        state = STATE_STOPPING
        stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS

    try:
        # Do not publish a signalable PID before our handlers are installed.
        control.sendall(b"ready")
        authorized = control.recv(16) == b"go"
        if authorized and state == STATE_RUNNING:
            try:
                command = [sys.executable, str(Path(__file__).with_name("exec_runner.py")),
                           str(limit)] + config["command"]
                env = dict(os.environ)
                if config.get("token"):
                    env["AGENTCORE_ATTEMPT_TOKEN"] = config["token"]
                if config.get("job"):
                    env["SUPERAGENT_JOB_ID"] = str(config["job"])
                if config.get("attempt"):
                    env["SUPERAGENT_ATTEMPT_ID"] = str(config["attempt"])
                env["SUPERAGENT_ATTEMPT_DIR"] = str(Path.cwd())
                # The runner inherits neither control/evidence descriptors nor
                # the lock. The supervisor retains it until its tree is reaped.
                proc = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout,
                                        stderr=sys.stderr, start_new_session=True, close_fds=True,
                                        env=env)
                child_started = True
            except Exception:
                state = STATE_STOPPING
                stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS
        else:
            state = STATE_STOPPING
            stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS
            code = -signal.SIGTERM

        quiescent = True
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
                    if state == STATE_RUNNING:
                        state = STATE_STOPPING
                        stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS

            if no_children:
                quiescent = True
                break

            # Bounded reaping deadline check: unkillable or surviving processes must not hang forever
            if state == STATE_STOPPING and stop_deadline is not None:
                if time.monotonic() >= stop_deadline:
                    denied = _kill_children()
                    remaining = []
                    try:
                        cpath = Path(f"/proc/self/task/{os.getpid()}/children")
                        if cpath.exists():
                            remaining = [int(p) for p in cpath.read_text().split()]
                    except Exception:
                        pass
                    unquiesced_pids = sorted(list(set(denied + remaining)))
                    quiescent = False
                    break

            readable, _, _ = select.select([control], [], [], .02)
            if readable:
                # Any control message or EOF is a stop request. A dead Worker
                # cannot leave an executing child orphaned behind a stale lease.
                control.recv(1024)
                if state == STATE_RUNNING:
                    state = STATE_STOPPING
                    stop_deadline = time.monotonic() + REAPING_DEADLINE_SECONDS

            if state == STATE_STOPPING:
                unquiesced_pids = _kill_children()

        # Emit evidence payload deterministically (ECHILD proof or bounded unquiesced report)
        proof = {name: config[name] for name in ("job", "attempt", "token", "nonce")}
        proof.update(quiescent=quiescent, proof_scope="subreaper_tree" if ready else "not_started",
                     supervisor_pid=os.getpid(), supervisor_start=process_start(os.getpid()),
                     child_started=child_started, exit_code=code)
        if not quiescent:
            try:
                cpath = Path(f"/proc/self/task/{os.getpid()}/children")
                if cpath.exists():
                    surviving = [int(p) for p in cpath.read_text().split()]
                    unquiesced_pids = sorted(list(set(unquiesced_pids + surviving)))
            except Exception:
                pass
            proof["unquiesced_pids"] = unquiesced_pids
        state = STATE_EVIDENCE_EMITTED
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
