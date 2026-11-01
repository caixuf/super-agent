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


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _installed_source_commit(executable):
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
            if resolved is None:
                return None
            interpreter = [resolved]
        probe = (
            "from importlib.metadata import distribution; "
            "print(distribution('hermes-feishu-streaming-card').read_text('direct_url.json') or '')"
        )
        proc = subprocess.run(interpreter + ["-c", probe], text=True,
                              capture_output=True, timeout=20)
        if proc.returncode != 0:
            return None
        direct_url = json.loads(proc.stdout)
        vcs = _mapping(direct_url.get("vcs_info"))
        repository = str(direct_url.get("url", "")).removesuffix(".git")
        if repository != "https://github.com/baileyh8/hermes-feishu-streaming-card":
            return None
        commit = vcs.get("commit_id")
        return commit if isinstance(commit, str) else None
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def evaluate_report(report, *, installed_commit=None, require_turn=True):
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

    checks = {
        "pinned_version": report.get("package_version") == lock["version"],
        "pinned_cli_source_commit": installed_commit == lock["commit"],
        "doctor_ok": diagnostics.get("status") == "ok",
        "hermes_compatibility": hermes.get("compatibility") == "full",
        "install_state": install.get("status") == "installed",
        "runtime_import": runtime_import.get("status") == "ok",
        "sidecar_healthy": runtime.get("sidecar_status") == "healthy",
        "runtime_ready": readiness.get("status") == "ready",
        "integrity_safe": integrity.get("mode") == "safe",
        "generation_matches": readiness.get("generation_match") is True,
    }
    if require_turn:
        checks.update({
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
        "reported_cli_source_commit": installed_commit,
        "reported_version": report.get("package_version"),
        "status": "ready" if not blockers else "blocked",
        "mode": "interaction_sample" if require_turn else "infrastructure_only",
        "checks": checks,
        "blockers": blockers,
        "notices": warnings,
        "manual_client_acceptance_required": [
            "first_card_rendering",
            "in_place_updates_and_single_terminal_card",
            "button_callback_resumes_same_turn",
            "no_duplicate_card_producers",
        ],
    }


def _doctor_command(args):
    executable = shutil.which(args.doctor_cli)
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

    result = evaluate_report(report, installed_commit=_installed_source_commit(command[0]),
                             require_turn=not args.infrastructure_only)
    if proc.returncode != 0 and "doctor_command_failed" not in result["blockers"]:
        result["blockers"].append("doctor_command_failed")
        result["status"] = "blocked"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
