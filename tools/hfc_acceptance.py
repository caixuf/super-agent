#!/usr/bin/env python3
"""Run HFC's read-only doctor and evaluate its pinned integration contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "integrations" / "hfc" / "lock.json"
UPSTREAM_REPOSITORY = "https://github.com/baileyh8/hermes-feishu-streaming-card"
_PROBE_DIRECT_URL = (
    "from importlib.metadata import distribution; "
    "print(distribution('hermes-feishu-streaming-card').read_text('direct_url.json') or '')"
)
_PROBE_VERSION = (
    "from importlib.metadata import version; "
    "print(version('hermes-feishu-streaming-card'))"
)
_PROBE_LOCAL_WHEEL = '''
import hashlib, json, pathlib, zipfile
from importlib.metadata import distribution
from urllib.parse import urlsplit, unquote
d = distribution('hermes-feishu-streaming-card')
meta = json.loads(d.read_text('direct_url.json') or '{}')
url = urlsplit(meta.get('url', ''))
if url.scheme != 'file' or url.netloc not in ('', 'localhost'):
    raise SystemExit(1)
wheel = pathlib.Path(unquote(url.path))
if not wheel.is_absolute() or wheel.suffix != '.whl' or not wheel.is_file():
    raise SystemExit(1)
with zipfile.ZipFile(wheel) as archive:
    checked = 0
    for entry in archive.infolist():
        name = pathlib.PurePosixPath(entry.filename)
        if entry.is_dir():
            continue
        if name.is_absolute() or '..' in name.parts or '\\\\' in entry.filename:
            raise SystemExit(1)
        if name.name == 'RECORD' and name.parent.name.endswith('.dist-info'):
            continue  # Installers regenerate RECORD and direct_url.json.
        installed = pathlib.Path(d.locate_file(entry.filename))
        if not installed.is_file() or installed.read_bytes() != archive.read(entry):
            raise SystemExit(1)
        checked += 1
    if not checked:
        raise SystemExit(1)
print(json.dumps({'kind': 'deployed_wheel', 'version': d.version,
                 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}))
'''


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _interpreter(executable):
    try:
        first = Path(executable).read_text(encoding="utf-8").splitlines()[0]
        if not first.startswith("#!"):
            return None
        interpreter = shlex.split(first[2:].strip())
        if not interpreter:
            return None
        if Path(interpreter[0]).name == "env":
            if len(interpreter) < 2:
                return None
            resolved = shutil.which(interpreter[1])
            return [resolved] if resolved else None
        return interpreter
    except (OSError, IndexError, UnicodeError):
        return None


def _probe(executable, code):
    interpreter = _interpreter(executable)
    if interpreter is None:
        return None
    try:
        proc = subprocess.run(interpreter + ["-c", code], text=True,
                              capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def installed_identity(executable):
    """Resolve what is installed, not what the lock wishes were installed.

    Two identities are legitimate: a git checkout of the upstream repository and
    a built wheel carrying reviewed source repairs. A wheel has no commit, so
    demanding one from a wheel install can only ever produce a permanent false red.
    """
    stdout = _probe(executable, _PROBE_DIRECT_URL)
    if stdout is None:
        return None
    try:
        direct_url = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    direct_url = _mapping(direct_url)
    commit = _mapping(direct_url.get("vcs_info")).get("commit_id")
    repository = str(direct_url.get("url", "")).removesuffix(".git")
    if repository == UPSTREAM_REPOSITORY and isinstance(commit, str):
        return {"kind": "upstream_pin", "commit": commit}
    archive = _mapping(direct_url.get("archive_info"))
    digest = str(archive.get("hash") or "")
    if not digest and _mapping(archive.get("hashes")).get("sha256"):
        digest = "sha256=" + archive["hashes"]["sha256"]
    if digest.startswith("sha256="):
        version = (_probe(executable, _PROBE_VERSION) or "").strip() or None
        return {"kind": "deployed_wheel", "wheel_sha256": digest[len("sha256="):],
                "version": version}
    if isinstance(commit, str):
        return {"kind": "upstream_pin", "commit": commit}
    # Some local-wheel installers omit archive_info hashes. Recover identity
    # only after verifying the installed payload against every wheel member.
    verified = _probe(executable, _PROBE_LOCAL_WHEEL)
    if verified:
        try:
            return json.loads(verified)
        except json.JSONDecodeError:
            return None
    return None


def accepted_identity(lock):
    """The identity this repository is willing to certify."""
    if _mapping(lock.get("identity")).get("accepted") == "deployed_wheel":
        record = _mapping(lock.get("deployed_build"))
        return {"kind": "deployed_wheel",
                "wheel_sha256": str(record.get("wheel_sha256") or ""),
                "version": str(record.get("version") or "")}
    return {"kind": "upstream_pin", "commit": str(lock.get("commit") or "")}


def _identity_matches(actual, expected):
    if not isinstance(actual, dict) or actual.get("kind") != expected["kind"]:
        return False
    if expected["kind"] == "upstream_pin":
        return bool(expected["commit"]) and actual.get("commit") == expected["commit"]
    return (bool(expected["wheel_sha256"])
            and actual.get("wheel_sha256") == expected["wheel_sha256"]
            and actual.get("version") == expected["version"])


def _delivery_safety(lock, known, identity_matched):
    """Delivery safety is either upstream-reviewed, or repaired with recorded evidence."""
    if not isinstance(known, list):
        return False, ["delivery_safety_review_missing"]
    if not known:
        return True, []
    record = _mapping(lock.get("deployed_build"))
    evidence = _mapping(record.get("evidence"))
    resolved = record.get("blockers_resolved_on_this_build")
    covered = evidence.get("covers")
    repaired = bool(
        identity_matched
        and isinstance(resolved, list) and isinstance(covered, list)
        and not set(known) - set(resolved)
        and not set(known) - set(covered)
        and str(evidence.get("result") or "").strip()
    )
    return repaired, known


def evaluate_report(report, *, installed_identity=None, installed_commit=None,
                    require_turn=True):
    """Return only bounded check IDs; never forward the upstream report."""
    report = _mapping(report)
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    diagnostics = _mapping(report.get("diagnostics"))
    hermes = _mapping(diagnostics.get("hermes"))
    install = _mapping(diagnostics.get("install_state"))
    runtime_import = _mapping(diagnostics.get("runtime_import"))
    runtime = _mapping(diagnostics.get("runtime"))
    readiness = _mapping(runtime.get("readiness"))
    integrity = _mapping(runtime.get("integrity"))
    timing = _mapping(runtime.get("timing"))

    expected = accepted_identity(lock)
    reported = installed_identity if _mapping(installed_identity).get("kind") else None
    identity_matched = _identity_matches(reported, expected)
    identity_check = ("recorded_build_identity" if expected["kind"] == "deployed_wheel"
                      else "pinned_cli_source_commit")
    checks = {
        "pinned_version": report.get("package_version") == lock["version"],
        "identity_resolvable": reported is not None,
        identity_check: identity_matched,
        "doctor_ok": diagnostics.get("status") == "ok",
        "hermes_compatibility": hermes.get("compatibility") == "full",
        "install_state": install.get("status") == "installed",
        "runtime_import": runtime_import.get("status") == "ok",
        "sidecar_healthy": runtime.get("sidecar_status") == "healthy",
        "runtime_ready": readiness.get("status") == "ready",
        "integrity_safe": integrity.get("mode") == "safe",
        "generation_matches": readiness.get("generation_match") is True,
    }
    known_blockers = lock.get("known_delivery_blockers")
    delivery_safety_reviewed, known_report = _delivery_safety(
        lock, known_blockers, identity_matched
    )
    if require_turn:
        checks.update({
            "no_known_delivery_loss": delivery_safety_reviewed,
            "turn_started_observed": _positive(timing.get("started_samples")),
            "first_answer_observed": _positive(timing.get("first_answer_samples")),
            "turn_completed_observed": _positive(timing.get("completed_samples")),
        })

    findings = diagnostics.get("findings")
    warnings = sorted({
        item["code"] for item in findings
        if isinstance(item, dict) and item.get("severity") in {"warning", "warn", "info"}
        and isinstance(item.get("code"), str)
    }) if isinstance(findings, list) else []
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "component": lock["distribution"],
        "expected_version": lock["version"],
        "expected_cli_source_commit": lock["commit"],
        "reported_cli_source_commit": _mapping(reported).get("commit"),
        "accepted_identity": expected,
        "reported_identity": reported,
        "reported_version": report.get("package_version"),
        "status": "ready" if not blockers else "blocked",
        "mode": "interaction_sample" if require_turn else "infrastructure_only",
        "checks": checks,
        "blockers": blockers,
        "notices": warnings,
        "known_delivery_blockers": known_report,
        # Automated checks only; the manual client criteria below remain required.
        "interaction_gate_passed": require_turn and not blockers and delivery_safety_reviewed,
        "manual_client_acceptance_required": [
            "first_card_rendering",
            "in_place_updates_and_single_terminal_card",
            "button_callback_resumes_same_turn",
            "no_duplicate_card_producers",
        ],
    }


def _recorded_cli():
    """Fall back to the recorded install when the CLI is not on PATH."""
    try:
        lock = json.loads(Path(LOCK).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    venv = _mapping(lock.get("deployed_build")).get("venv")
    if not isinstance(venv, str) or not venv:
        return None
    candidate = Path(venv).expanduser() / "bin" / "hermes-feishu-card"
    return str(candidate) if candidate.is_file() else None


def _doctor_command(args):
    executable = shutil.which(args.doctor_cli)
    if executable is None and Path(args.doctor_cli).name == args.doctor_cli:
        executable = _recorded_cli()
    if executable is None:
        return None
    command = [executable, "doctor", "--config", str(args.config),
               "--hermes-dir", str(args.hermes_dir), "--support-report"]
    if args.hermes_home:
        command.extend(["--hermes-home", str(args.hermes_home)])
    if args.env_file:
        command.extend(["--env-file", str(args.env_file)])
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hermes-dir", type=Path, required=True)
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--doctor-cli", default="hermes-feishu-card")
    parser.add_argument("--infrastructure-only", action="store_true",
                        help="check installation/runtime only; does not satisfy turn acceptance")
    args = parser.parse_args(argv)

    command = _doctor_command(args)
    if command is None:
        result = {"schema_version": 1, "status": "blocked",
                  "blockers": ["hfc_cli_not_found"]}
        print(json.dumps(result, sort_keys=True))
        return 1
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=120)
        report = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, UnicodeError):
        result = {"schema_version": 1, "status": "blocked",
                  "blockers": ["hfc_doctor_report_unavailable"]}
        print(json.dumps(result, sort_keys=True))
        return 1

    result = evaluate_report(report, installed_identity=installed_identity(command[0]),
                             require_turn=not args.infrastructure_only)
    if proc.returncode != 0 and "doctor_command_failed" not in result["blockers"]:
        result["blockers"].append("doctor_command_failed")
        result["status"] = "blocked"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
