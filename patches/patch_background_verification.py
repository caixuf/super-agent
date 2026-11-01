#!/usr/bin/env python3
"""Record completed background commands as verification evidence.

``terminal(background=true)`` only returns a spawn receipt.  A later
``process_manage`` wait/poll receives the host's authoritative exit status, but
before this patch that result never reached the verification ledger.  The patch
therefore wires the existing classifier/recorder at the completed process
observation boundary and adds a start-time freshness fence to that recorder.

Safety properties:
* command, cwd, owner/session, completion status and start time all come from the
  process registry record; model arguments are never used to reconstruct them;
* result status/exit code/reason must exactly agree with the live process record;
* only a live tracked session with ``status=exited`` and an integer exit code is
  eligible (never spawn/running/timeout/lost/unknown);
* the caller's task and session must exactly match the recorded owner/spawner;
* no edit in the workspace root by any session may be newer than the command start;
* output is read from the complete in-memory buffer.  If the rolling buffer
  itself dropped bytes, no evidence is recorded;
* repeated wait/poll observations of one process record at most one event;
* evidence failures never replace or break the process_manage result.

Use: python3 patch_background_verification.py --check | --apply [--agent-dir DIR]
Exit: 0 = present/applied; 1 = absent under --check; 2 = source anchor drift.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

EVIDENCE_REL = Path("agent/verification_evidence.py")
REGISTRY_REL = Path("tools/process_registry.py")

EVIDENCE_OLD = '''def record_terminal_result(
    *, command: str, cwd: str | Path | None, session_id: str | None, exit_code: int, output: str = ""
) -> Optional[dict[str, Any]]:
    """Record a foreground terminal result when it is verification evidence."""
    if not _ledger_enabled():
        return None
    evidence = classify_verification_command(command, cwd=cwd, session_id=session_id, exit_code=exit_code, output=output)
    return None if evidence is None else _insert_evidence(evidence)
'''
EVIDENCE_NEW = '''def record_terminal_result(
    *, command: str, cwd: str | Path | None, session_id: str | None, exit_code: int, output: str = "",
    started_at: float | str | None = None,
) -> Optional[dict[str, Any]]:
    """Record a terminal result when it is fresh, attributable verification evidence.

    ``started_at`` is used by background-process observation: the command must
    have started at or after every recorded edit in the workspace root.
    Foreground callers omit it.
    """
    if not _ledger_enabled():
        return None
    evidence = classify_verification_command(command, cwd=cwd, session_id=session_id, exit_code=exit_code, output=output)
    return None if evidence is None else _insert_evidence(evidence, started_at=started_at)
'''

TRANSACTION_OLD = '''def _transaction():
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect())
'''
TRANSACTION_NEW = '''def _transaction(*, immediate: bool = False):
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect(), immediate=immediate)
'''

INSERT_OLD = '''def _insert_evidence(evidence: VerificationEvidence) -> dict[str, Any]:
    """Insert a classified evidence row and repoint the workspace state."""
    created_at = _utc_now()
    e = evidence
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
'''
INSERT_NEW = '''def _insert_evidence(
    evidence: VerificationEvidence, *, started_at: float | str | None = None,
) -> Optional[dict[str, Any]]:
    """Insert a classified evidence row and repoint the workspace state.

    When ``started_at`` is supplied, the insert is refused if ANY session has
    edited this workspace root since the command started.  The root-wide state
    check and the insert share one immediate transaction, so a competing edit
    cannot slip between the freshness decision and the successful-evidence write.
    """
    created_at = _utc_now()
    e = evidence
    if started_at is not None:
        try:
            started = (
                datetime.fromtimestamp(float(started_at), UTC).isoformat()
                if isinstance(started_at, (int, float)) else str(started_at)
            )
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    with _DB_LOCK, _transaction(immediate=started_at is not None) as conn:
        if started_at is not None:
            state = conn.execute(
                "SELECT MAX(last_edit_at) AS last_edit_at FROM verification_state WHERE root = ?",
                (e.root,),
            ).fetchone()
            if state is not None and state["last_edit_at"] and started < state["last_edit_at"]:
                return None
            # A newer pass clears last_edit_at.  The event remains the durable
            # high-water mark, so an older process cannot become fresh after a
            # competing session edited and re-verified the same root.
            newer_event = conn.execute(
                "SELECT 1 FROM verification_events WHERE root = ? AND created_at > ? LIMIT 1",
                (e.root, started),
            ).fetchone()
            if newer_event is not None:
                return None
        cur = conn.execute(
'''

MARKER = "_record_completed_verification_evidence"
REG_HELPER = '''# A completed process is verification evidence only when the observation and
# the process record prove the same owner, exact exit, and complete output.
_VERIFICATION_EVIDENCE_ATTR = "_background_verification_recorded"


def _record_completed_verification_evidence(
    *, action: str, session_id: str, result: dict, caller_session_id: str, caller_task_id: str,
) -> None:
    """Bridge a trusted completed-process snapshot into the host verification ledger.

    This is deliberately fail-closed.  The model supplies only the process id;
    every fact used as evidence comes from the registry's ProcessSession and the
    caller identity is supplied by the tool dispatcher, not the tool arguments.
    """
    try:
        if action not in ("wait", "poll") or not isinstance(result, dict):
            return
        if result.get("status") != "exited" or result.get("completion_reason") != "exited":
            return
        exit_code = result.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            return
        if not caller_session_id or not caller_task_id:
            return
        session = process_registry.get(str(session_id or ""))
        if session is None or not session.exited:
            return
        # Recovered receipts are informational snapshots; they do not prove that
        # this process was observed by its spawning session in this runtime.
        with process_registry._lock:
            live = process_registry._running.get(session.id) or process_registry._finished.get(session.id)
        if live is not session:
            return
        # The handler JSON must agree with the live record, not merely look valid.
        if exit_code != session.exit_code:
            return
        if str(session.completion_reason or "") != "exited":
            return
        if str(session.parent_session_id or "") != caller_session_id:
            return
        if str(session.owner_task_id or "") != caller_task_id:
            return

        with session._lock:
            if getattr(session, _VERIFICATION_EVIDENCE_ATTR, False):
                return
            # Completion responses carry a bounded tail.  The registry buffer is the
            # source of truth; if it lost bytes too, the output is not complete.
            total = int(getattr(session, "total_output_chars", 0) or 0)
            output = session.output_buffer or ""
            if total > len(output):
                return
            command = session.command or ""
            cwd = session.cwd or ""
            started_at = float(session.started_at or 0.0)
            if not command or not cwd or started_at <= 0:
                return
            setattr(session, _VERIFICATION_EVIDENCE_ATTR, True)
            try:
                from agent.redact import redact_sensitive_text, redact_terminal_output
                from agent.verification_evidence import record_terminal_result
                safe_command = redact_sensitive_text(command, code_file=True)
                safe_output = redact_terminal_output(output, command)
                record_terminal_result(
                    command=safe_command, cwd=cwd, session_id=caller_session_id,
                    exit_code=exit_code, output=safe_output, started_at=started_at,
                )
            except Exception:
                delattr(session, _VERIFICATION_EVIDENCE_ATTR)
                raise
    except Exception:
        # Verification is an observer.  It must never turn a valid process
        # result into a tool error or suppress a retryable observation.
        logger.debug("background verification evidence recording failed", exc_info=True)


'''

HANDLE_OLD = '''        handler, redact = _SESSION_ACTIONS[action]
        from tools.process_registry_wait_policy import apply_wait_poll_policy
        result = apply_wait_poll_policy(action, session_id, handler(session_id, args))
        return json.dumps(_redact_process_result(result) if redact else result, ensure_ascii=False)
'''
HANDLE_NEW = '''        handler, redact = _SESSION_ACTIONS[action]
        from tools.process_registry_wait_policy import apply_wait_poll_policy
        result = apply_wait_poll_policy(action, session_id, handler(session_id, args))
        if redact:
            result = _redact_process_result(result)
        try:
            _record_completed_verification_evidence(
                action=action, session_id=session_id, result=result,
                caller_session_id=str(kw.get("session_id") or ""),
                caller_task_id=str(kw.get("task_id") or ""),
            )
        except Exception:
            logger.debug("background verification bridge failed", exc_info=True)
        return json.dumps(result, ensure_ascii=False)
'''


def _agent_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"{label} anchor matched {count} times (expected 1)")
    return source.replace(old, new, 1)


def transform_evidence(source: str) -> str:
    """Apply all evidence changes, or prove the complete patch is already present.

    A mixture of old and new fragments (including a hand-edited/partial patch)
    is rejected rather than guessed at.  This makes ``--check`` incapable of
    accepting the patch from one convenient marker string.
    """
    new_counts = (source.count(EVIDENCE_NEW), source.count(TRANSACTION_NEW), source.count(INSERT_NEW))
    old_counts = (source.count(EVIDENCE_OLD), source.count(TRANSACTION_OLD), source.count(INSERT_OLD))
    if new_counts == (1, 1, 1):
        if any(old_counts):
            raise ValueError("partial verification evidence patch: old and new anchors coexist")
        return source
    partial_markers = (
        "started_at: float | str | None = None",
        "_transaction(immediate=started_at is not None)",
        "SELECT MAX(last_edit_at) AS last_edit_at FROM verification_state WHERE root = ?",
    )
    if any(new_counts) or any(marker in source for marker in partial_markers):
        raise ValueError("partial verification evidence patch: expected all three new fragments exactly once")
    source = _replace_once(source, EVIDENCE_OLD, EVIDENCE_NEW, "record_terminal_result")
    source = _replace_once(source, TRANSACTION_OLD, TRANSACTION_NEW, "_transaction")
    return _replace_once(source, INSERT_OLD, INSERT_NEW, "_insert_evidence")


def transform_registry(source: str) -> str:
    """Apply helper + dispatcher wiring, or prove both are complete exactly once."""
    helper_count = source.count(REG_HELPER)
    handle_new_count = source.count(HANDLE_NEW)
    handle_old_count = source.count(HANDLE_OLD)
    if (helper_count, handle_new_count) == (1, 1):
        if handle_old_count:
            raise ValueError("partial process registry patch: old and new handler coexist")
        return source
    # The pristine source has exactly one old handler and neither new fragment.
    if (helper_count, handle_new_count, handle_old_count) == (0, 0, 1) and MARKER not in source:
        source = _replace_once(
            source, "def _handle_process(args, **kw):",
            REG_HELPER + "def _handle_process(args, **kw):", "process handler",
        )
        return _replace_once(source, HANDLE_OLD, HANDLE_NEW, "process session action return")
    raise ValueError("partial process registry patch: expected helper and handler fragments exactly once")


def _load(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    parser.add_argument("--agent-dir", default=None)
    args = parser.parse_args()

    root = _agent_dir(args.agent_dir)
    evidence_path = root / EVIDENCE_REL
    registry_path = root / REGISTRY_REL
    try:
        evidence_before = _load(evidence_path)
        registry_before = _load(registry_path)
        evidence_after = transform_evidence(evidence_before)
        registry_after = transform_registry(registry_before)
        compile(evidence_after, str(evidence_path), "exec")
        compile(registry_after, str(registry_path), "exec")
    except ValueError as exc:
        print(f"[FAIL] background verification patch anchor drift: {exc}")
        return 2
    except SyntaxError as exc:
        print(f"[FAIL] background verification patch produced invalid Python: {exc}")
        return 2

    present = evidence_after == evidence_before and registry_after == registry_before
    if present:
        print("[PASS] background verification evidence patch is installed")
        return 0
    if args.check:
        print("[FAIL] background verification evidence patch is not installed")
        return 1

    # Both replacement plans and syntax checks succeed before either file is
    # touched.  A drifted anchor therefore has zero write side effects.
    for path, before, after in (
        (evidence_path, evidence_before, evidence_after),
        (registry_path, registry_before, registry_after),
    ):
        if before == after:
            continue
        sibling = path.with_name(path.name + ".background-verification-tmp")
        sibling.write_text(after, encoding="utf-8")
        os.replace(sibling, path)
    print("[PASS] background verification evidence patch installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
