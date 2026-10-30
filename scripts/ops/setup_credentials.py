#!/usr/bin/env python3
"""Zero-secret credentials setup wizard with interactive guidance and validation."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

_FEISHU_APP_ID_RE = re.compile(r"^cli_[a-z0-9]{10,40}$", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://[a-zA-Z0-9_.-]+(:\d+)?(/.*)?$")


def mask_secret(value: str) -> str:
    """Mask a secret value for secure display without leaking sensitive characters."""
    if not value:
        return "<empty>"
    if len(value) <= 6:
        return "***"
    return value[:4] + "..." + "*" * 4


def validate_feishu_credentials(app_id: str, app_secret: str) -> Tuple[bool, str]:
    """Validate Feishu App ID and Secret format."""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id:
        return False, "Feishu App ID cannot be empty"
    if not _FEISHU_APP_ID_RE.match(app_id):
        # Warn or reject if format does not match cli_...
        if not re.match(r"^[a-zA-Z0-9_-]{6,64}$", app_id):
            return False, f"Invalid Feishu App ID format: {app_id}"
    if not app_secret:
        return False, "Feishu App Secret cannot be empty"
    if len(app_secret) < 16:
        return False, "Feishu App Secret is too short (expected >= 16 characters)"
    return True, "valid"


def validate_model_endpoint(base_url: str, api_key: str) -> Tuple[bool, str]:
    """Validate model base URL and API key format."""
    base_url = (base_url or "").strip()
    api_key = (api_key or "").strip()
    if base_url and not _URL_RE.match(base_url):
        return False, f"Invalid base_url format: {base_url}"
    if not api_key:
        return False, "API key cannot be empty"
    return True, "valid"


def probe_model_endpoint(base_url: str, api_key: str, timeout: float = 3.0) -> Tuple[bool, str]:
    """Optional online connectivity check against OpenAI-compatible /v1/models endpoint."""
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True, "Endpoint reachable and credentials accepted (HTTP 200)"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            return False, f"Authentication rejected by endpoint: HTTP {err.code}"
        return False, f"HTTP {err.code} from {url}"
    except Exception as exc:
        return False, f"Connection failed: {type(exc).__name__}: {exc}"


def update_env_file(
    target_path: Path,
    updates: Dict[str, str],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Atomically and securely update key-value pairs in an environment file with 0600 permissions."""
    if target_path.is_symlink():
        raise ValueError(f"Refusing to write to symlink: {target_path}")

    parent_dir = target_path.parent
    if not parent_dir.exists():
        parent_dir.mkdir(parents=True, mode=0o700)

    # Read existing content if file exists
    existing_lines: List[str] = []
    seen_keys: set[str] = set()
    if target_path.exists():
        if target_path.is_symlink():
            raise ValueError(f"Target is a symlink: {target_path}")
        existing_lines = target_path.read_text(encoding="utf-8").splitlines()

    new_lines: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key, _, _ = line.partition("=")
            key = key.strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                seen_keys.add(key)
                continue
        new_lines.append(line)

    # Append any remaining keys
    for k, v in updates.items():
        if k not in seen_keys:
            new_lines.append(f"{k}={v}")

    final_content = "\n".join(new_lines).strip() + "\n"

    if dry_run:
        return {
            "ok": True,
            "target": str(target_path),
            "updated_keys": list(updates.keys()),
            "dry_run": True,
        }

    # Atomic write with 0600 permissions
    fd, temp_name = tempfile.mkstemp(prefix=".env-wizard-", dir=parent_dir)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(final_content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    # Double check final permissions
    target_path.chmod(0o600)
    return {
        "ok": True,
        "target": str(target_path),
        "updated_keys": list(updates.keys()),
        "dry_run": False,
    }


def resolve_profile_env_path(profile: str) -> Path:
    """Resolve standard hermes profile env file location."""
    home = Path.home()
    return home / ".hermes" / "profiles" / profile / ".env"


def run_wizard(
    *,
    profile: Optional[str] = None,
    target_file: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    model_base_url: Optional[str] = None,
    model_api_key: Optional[str] = None,
    validate: bool = False,
    dry_run: bool = False,
    interactive: bool = True,
) -> int:
    """Run interactive or CLI credentials setup wizard."""
    print("======================================================================")
    print(" Super Agent: Zero-Secret Credentials Setup Wizard")
    print("======================================================================")

    if target_file:
        dest_path = Path(target_file).expanduser().resolve()
    elif profile:
        dest_path = resolve_profile_env_path(profile)
    elif interactive:
        prof = input("Enter Hermes profile name [default]: ").strip() or "default"
        dest_path = resolve_profile_env_path(prof)
    else:
        dest_path = resolve_profile_env_path("default")

    print(f"Target Configuration File: {dest_path}")

    # Collect inputs
    if not app_id and interactive:
        app_id = input("Enter Feishu App ID (cli_...): ").strip()
    if not app_secret and interactive:
        app_secret = getpass.getpass("Enter Feishu App Secret (input hidden): ").strip()

    if not model_base_url and interactive:
        model_base_url = input("Enter Model Base URL [http://127.0.0.1:4000/v1]: ").strip()
    if not model_api_key and interactive:
        model_api_key = getpass.getpass("Enter Model API Key (input hidden): ").strip()

    updates: Dict[str, str] = {}
    if app_id:
        val_ok, msg = validate_feishu_credentials(app_id, app_secret or "dummy_length_secret_123")
        if not val_ok:
            print(f"[-] Validation Error (Feishu App ID): {msg}", file=sys.stderr)
            return 1
        updates["FEISHU_APP_ID"] = app_id

    if app_secret:
        val_ok, msg = validate_feishu_credentials(app_id or "cli_a1b2c3d4e5f6", app_secret)
        if not val_ok:
            print(f"[-] Validation Error (Feishu App Secret): {msg}", file=sys.stderr)
            return 1
        updates["FEISHU_APP_SECRET"] = app_secret

    if model_base_url:
        updates["OPENAI_BASE_URL"] = model_base_url
        updates["MODEL_BASE_URL"] = model_base_url

    if model_api_key:
        updates["OPENAI_API_KEY"] = model_api_key
        updates["MODEL_API_KEY"] = model_api_key

    if not updates:
        print("[-] No credential updates provided. Exiting without changes.")
        return 0

    print("\n[+] Credentials to configure (values masked):")
    for k, v in updates.items():
        print(f"    - {k}: {mask_secret(v)}")

    if validate and model_base_url and model_api_key:
        print("\n[*] Validating model connectivity...")
        ok, probe_msg = probe_model_endpoint(model_base_url, model_api_key)
        if ok:
            print(f"[+] Model Endpoint Validation PASSED: {probe_msg}")
        else:
            print(f"[-] Model Endpoint Validation FAILED: {probe_msg}", file=sys.stderr)
            if not dry_run and interactive:
                cont = input("Proceed anyway? (y/N): ").strip().lower()
                if cont != "y":
                    return 1

    res = update_env_file(dest_path, updates, dry_run=dry_run)
    if dry_run:
        print("\n[+] [Dry Run] Config generated successfully. No files modified.")
    else:
        print(f"\n[+] Successfully written to {dest_path} with mode 0600.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Hermes profile name")
    parser.add_argument("--target-file", help="Explicit path to target .env file")
    parser.add_argument("--app-id", help="Feishu App ID (cli_...)")
    parser.add_argument("--app-secret", help="Feishu App Secret")
    parser.add_argument("--model-base-url", help="Model Base URL")
    parser.add_argument("--model-api-key", help="Model API Key")
    parser.add_argument("--validate", action="store_true", help="Probe endpoints for live validation")
    parser.add_argument("--dry-run", action="store_true", help="Check validity without writing file")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for missing inputs")
    args = parser.parse_args(argv)

    interactive = not args.non_interactive and sys.stdin.isatty()
    return run_wizard(
        profile=args.profile,
        target_file=args.target_file,
        app_id=args.app_id,
        app_secret=args.app_secret,
        model_base_url=args.model_base_url,
        model_api_key=args.model_api_key,
        validate=args.validate,
        dry_run=args.dry_run,
        interactive=interactive,
    )


if __name__ == "__main__":
    sys.exit(main())
