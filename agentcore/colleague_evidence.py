"""Validate an internal colleague result before creating an untrusted handoff.

The worker is trusted to capture its source snapshots. This module verifies that
the report refers to its own completed job, attempt and recorded snapshots; it
does not establish that an inference is correct or grant another role access.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from agentcore.analysis_runner import AnalysisError, validate_report

_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REPORT = 256 * 1024
_MAX_MANIFEST = 8 * 1024 * 1024
_MAX_SUMMARY = 1200


class HandoffEvidenceError(ValueError):
    """A result cannot be safely used as handoff context."""


def _read_file(directory, name, maximum):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise HandoffEvidenceError("artifact_budget_or_type")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            if (len(data) > maximum or
                    (before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino)):
                raise HandoffEvidenceError("artifact_changed")
            return data
    except OSError as exc:
        raise HandoffEvidenceError("artifact_unreadable") from exc


def verify_internal_result(pool, job_id):
    """Return a bounded handoff from one completed, unaddressed internal job.

    The caller supplies an administrator-selected pool, never a model-supplied
    path. Each downstream worker must independently re-read its authorized
    sources. No source text or artifact path is returned as authority.
    """
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise HandoffEvidenceError("invalid_job")
    with pool.db() as db:
        row = db.execute("SELECT status,attempt,payload,result,origin FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "completed" or row["origin"] is not None:
        raise HandoffEvidenceError("internal_completed_job_required")
    attempt = row["attempt"]
    if not isinstance(attempt, str) or not _ID.fullmatch(attempt):
        raise HandoffEvidenceError("invalid_attempt")
    try:
        payload = json.loads(row["payload"])
        receipt = json.loads(row["result"])
    except (ValueError, TypeError) as exc:
        raise HandoffEvidenceError("invalid_job_record") from exc
    result = receipt.get("result") if isinstance(receipt, dict) and receipt.get("ok") is True else None
    if (not isinstance(result, dict) or result.get("job") != job_id or result.get("attempt") != attempt
            or result.get("verification") not in ("citation_traceability_only", "dev_traceability_v1")):
        raise HandoffEvidenceError("receipt_identity_mismatch")
    verification_mode = result.get("verification")
    try:
        root = os.open(Path(pool.root) / "attempts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open(attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                report_raw = _read_file(directory, "report.json", _MAX_REPORT)
                manifest_raw = _read_file(directory, "manifest.json", _MAX_MANIFEST)
            finally:
                os.close(directory)
        finally:
            os.close(root)
        report, manifest = json.loads(report_raw), json.loads(manifest_raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise HandoffEvidenceError("invalid_artifact") from exc

    if verification_mode == "dev_traceability_v1":
        if (not isinstance(manifest, dict) or manifest.get("job") != job_id or manifest.get("attempt") != attempt
                or manifest.get("verification") != "dev_traceability_v1"):
            raise HandoffEvidenceError("manifest_identity_mismatch")
        if (not isinstance(report, dict) or report.get("job") != job_id or report.get("attempt") != attempt
                or report.get("verification") != "dev_traceability_v1"):
            raise HandoffEvidenceError("report_receipt_mismatch")
        summary_text = (report.get("summary") or "")[:_MAX_SUMMARY]
        return {
            "attempt_id": attempt,
            "report_sha256": hashlib.sha256(report_raw).hexdigest(),
            "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "summary": summary_text,
            "verification": "dev_traceability_v1",
            "worktree": manifest.get("worktree"),
            "modified_files": manifest.get("modified_files", []),
            "success": report.get("success", False),
        }

    if (not isinstance(manifest, dict) or manifest.get("job") != job_id or manifest.get("attempt") != attempt
            or manifest.get("verification") != "citation_traceability_only"):
        raise HandoffEvidenceError("manifest_identity_mismatch")
    snapshots, sources = manifest.get("sources"), payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(snapshots, list) or not isinstance(sources, list) or not 1 <= len(snapshots) == len(sources) <= 8:
        raise HandoffEvidenceError("snapshot_mismatch")
    for index, (snapshot, source) in enumerate(zip(snapshots, sources), 1):
        if (not isinstance(snapshot, dict) or not isinstance(source, dict)
                or snapshot.get("source_id") != "source_" + str(index)
                or snapshot.get("path") != source.get("path")
                or snapshot.get("start_line") != source.get("start_line", 1)
                or ("end_line" in source and snapshot.get("end_line") != source["end_line"])
                or not isinstance(snapshot.get("sha256"), str) or not _SHA.fullmatch(snapshot["sha256"])):
            raise HandoffEvidenceError("snapshot_mismatch")
    limitations = report.get("limitations") if isinstance(report, dict) else None
    provenance_note = "Citations were checked against snapshots; causal correctness requires review."
    if not isinstance(limitations, list) or not limitations or limitations[-1] != provenance_note:
        raise HandoffEvidenceError("report_limitations_missing")
    try:
        checked = validate_report({**report, "limitations": limitations[:-1]}, snapshots)
    except (AnalysisError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HandoffEvidenceError("report_citations_invalid") from exc
    if (not isinstance(report, dict) or report.get("verification") != "citation_traceability_only"
            or result.get("summary") != checked["conclusion"][:2000]
            or result.get("claim_count") != len(checked["claims"])):
        raise HandoffEvidenceError("report_receipt_mismatch")
    return {"attempt_id": attempt, "report_sha256": hashlib.sha256(report_raw).hexdigest(),
            "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "summary": checked["conclusion"][:_MAX_SUMMARY],
            "verification": "citation_traceability_only"}
