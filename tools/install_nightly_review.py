#!/usr/bin/env python3
"""Check or explicitly add the generic nightly review to a Hermes profile."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "runtime" / "cron" / "nightly-self-review.json"
JOB_NAME = "nightly-session-review"
LEGACY_NAME = "夜间会话复盘与改进"
MARKER = "[nightly-self-review-template:v1]"
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z", re.ASCII)


class InstallError(ValueError):
    """Stable, non-sensitive install/check failure."""


def _absolute_dir(raw: str, label: str) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise InstallError("invalid_" + label)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError("invalid_" + label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError:
        raise InstallError(label + "_unavailable") from None
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError(label + "_unavailable")
    return resolved


def _hermes_executable(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or any(ord(c) < 32 for c in value):
        raise InstallError("invalid_hermes_command")
    if os.path.isabs(value):
        path = Path(value)
        if ".." in path.parts:
            raise InstallError("invalid_hermes_command")
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise InstallError("hermes_unavailable") from None
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise InstallError("hermes_unavailable")
        return str(resolved)
    if value != Path(value).name or value in {".", ".."}:
        raise InstallError("invalid_hermes_command")
    resolved = shutil.which(value)
    if not resolved:
        raise InstallError("hermes_unavailable")
    return resolved


def _read_template() -> dict:
    try:
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InstallError("template_unavailable") from None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise InstallError("invalid_template")
    job = data[0]
    if (job.get("name") != JOB_NAME or job.get("notify") is not False
            or not isinstance(job.get("prompt"), str) or MARKER not in job["prompt"]):
        raise InstallError("invalid_template")
    return job


def _schedule(time_text: str) -> str:
    if not isinstance(time_text, str) or not _TIME.fullmatch(time_text):
        raise InstallError("invalid_time")
    hour, minute = (int(part) for part in time_text.split(":"))
    return f"{minute} {hour} * * *"


def _check_timezone(executable: str, profile_home: Path, expected: str, runner=subprocess.run) -> str:
    try:
        ZoneInfo(expected)
    except (ZoneInfoNotFoundError, TypeError):
        raise InstallError("invalid_timezone") from None
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile_home)
    # Hermes cron uses the host/profile timezone, not a per-job timezone. This only reads
    # profile config; a running service may also set HERMES_TIMEZONE independently.
    env.pop("HERMES_TIMEZONE", None)
    try:
        result = runner([executable, "config", "get", "timezone", "--json"], cwd=str(REPO), env=env,
                        text=True, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("timezone_query_failed") from None
    if result.returncode != 0:
        raise InstallError("timezone_query_failed")
    try:
        configured = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise InstallError("timezone_query_invalid") from None
    if not isinstance(configured, str) or not configured:
        raise InstallError("profile_timezone_unconfigured")
    try:
        ZoneInfo(configured)
    except (ZoneInfoNotFoundError, TypeError):
        raise InstallError("profile_timezone_invalid") from None
    if configured != expected:
        raise InstallError("profile_timezone_mismatch")
    override = os.environ.get("HERMES_TIMEZONE", "").strip()
    if override and override != expected:
        raise InstallError("caller_timezone_override_mismatch")
    return configured


def _read_jobs(profile_home: Path) -> list[dict]:
    cron_dir = profile_home / "cron"
    if cron_dir.exists() or cron_dir.is_symlink():
        try:
            info = cron_dir.lstat()
        except OSError:
            raise InstallError("cron_directory_unavailable") from None
        if not stat.S_ISDIR(info.st_mode):
            raise InstallError("cron_directory_unsafe")
    path = cron_dir / "jobs.json"
    if not path.exists() and not path.is_symlink():
        return []
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            raise InstallError("cron_store_unsafe")
        data = json.loads(path.read_text(encoding="utf-8"))
    except InstallError:
        raise
    except (OSError, ValueError):
        raise InstallError("cron_store_invalid") from None
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or any(not isinstance(item, dict) for item in jobs):
        raise InstallError("cron_store_invalid")
    return jobs


def _job_schedule(value):
    return value.get("expr") if isinstance(value, dict) else value


def _existing(jobs: list[dict], *, prompt: str, schedule: str, workdir: Path) -> str | None:
    # The known pre-template deployment has no marker. Never silently duplicate or migrate it.
    if any(job.get("name") == LEGACY_NAME for job in jobs):
        return "LEGACY_CONFLICT"
    matches = [job for job in jobs if job.get("name") == JOB_NAME]
    marked = [job for job in jobs if MARKER in str(job.get("prompt") or "")]
    if len(matches) > 1 or len(marked) > 1:
        return "CONFLICT"
    if marked and not matches:
        return "CONFLICT"
    if not matches:
        return None
    current = matches[0]
    if (current.get("enabled", True) is False or _job_schedule(current.get("schedule")) != schedule
            or current.get("prompt") != prompt or current.get("workdir") != str(workdir)
            or current.get("deliver") not in (None, "local")):
        return "CONFLICT"
    return "EXISTS"


def _profile_lock(profile_home: Path):
    cron_dir = profile_home / "cron"
    cron_dir.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(cron_dir.lstat().st_mode):
        raise InstallError("cron_directory_unsafe")
    path = cron_dir / ".nightly-self-review-install.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1:
            os.close(fd)
            raise InstallError("install_lock_unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except InstallError:
        raise
    except OSError:
        raise InstallError("install_lock_unavailable") from None


def install_or_check(*, profile: str, profile_home: str, workdir: str, timezone: str,
                     at: str = "01:00", install: bool = False, hermes: str = "hermes",
                     runner=subprocess.run) -> tuple[str, str]:
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise InstallError("invalid_profile")
    home = _absolute_dir(profile_home, "profile_home")
    cwd = _absolute_dir(workdir, "workdir")
    executable = _hermes_executable(hermes)
    schedule = _schedule(at)
    template = _read_template()
    prompt = template["prompt"].replace("<profile>", str(home))
    jobs = _read_jobs(home)
    if any(job.get("name") == LEGACY_NAME for job in jobs):
        return "LEGACY_CONFLICT", "legacy job exists; inspect and migrate manually, nothing changed"
    _check_timezone(executable, home, timezone, runner=runner)
    existing = _existing(jobs, prompt=prompt, schedule=schedule, workdir=cwd)
    if existing == "LEGACY_CONFLICT":
        return "LEGACY_CONFLICT", "legacy job exists; inspect and migrate manually, nothing changed"
    if existing == "EXISTS":
        return "EXISTS", "matching job already present; left unchanged"
    if existing == "CONFLICT":
        raise InstallError("existing_job_conflict; nothing changed")
    if not install:
        return "MISSING", "no matching job; pass --install to add it"
    fd = _profile_lock(home)
    try:
        existing = _existing(_read_jobs(home), prompt=prompt, schedule=schedule, workdir=cwd)
        if existing == "LEGACY_CONFLICT":
            return "LEGACY_CONFLICT", "legacy job exists; inspect and migrate manually, nothing changed"
        if existing == "EXISTS":
            return "EXISTS", "matching job already present; left unchanged"
        if existing == "CONFLICT":
            raise InstallError("existing_job_conflict; nothing changed")
        env = os.environ.copy()
        env["HERMES_HOME"] = str(home)
        args = [executable, "cron", "create", schedule, prompt, "--name", JOB_NAME,
                "--deliver", "local", "--workdir", str(cwd)]
        try:
            result = runner(args, cwd=str(REPO), env=env, text=True, capture_output=True,
                            timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            after = _existing(_read_jobs(home), prompt=prompt, schedule=schedule, workdir=cwd)
            if after in {"EXISTS", "LEGACY_CONFLICT"}:
                return "UNKNOWN", "Hermes CLI outcome unknown; inspect with hermes cron list before retry"
            raise InstallError("hermes_create_outcome_unknown") from None
        after = _existing(_read_jobs(home), prompt=prompt, schedule=schedule, workdir=cwd)
        if result.returncode != 0:
            if after == "EXISTS":
                return "UNKNOWN", "Hermes CLI returned failure after creating the job; inspect before retry"
            raise InstallError("hermes_create_failed")
        if after != "EXISTS":
            return "UNKNOWN", "CLI returned success but job did not match; inspect Hermes cron state"
        return "ADDED_PENDING_RUNTIME_TZ_CHECK", "job added via Hermes CLI; verify the active gateway HERMES_TIMEZONE override"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Hermes profile name (identity label only)")
    parser.add_argument("--profile-home", required=True, help="absolute HERMES_HOME for that profile")
    parser.add_argument("--workdir", required=True, help="absolute existing working directory")
    parser.add_argument("--timezone", required=True, help="expected IANA timezone; must match profile config")
    parser.add_argument("--at", default="01:00", help="local time in profile timezone (default 01:00)")
    parser.add_argument("--hermes", default="hermes", help="Hermes CLI executable")
    parser.add_argument("--install", action="store_true", help="explicitly add the job if absent")
    args = parser.parse_args(argv)
    try:
        status, detail = install_or_check(profile=args.profile, profile_home=args.profile_home,
            workdir=args.workdir, timezone=args.timezone, at=args.at, install=args.install,
            hermes=args.hermes)
        print(f"{status} {detail}")
        return 0 if status in {"EXISTS", "ADDED_PENDING_RUNTIME_TZ_CHECK"} else 1
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
