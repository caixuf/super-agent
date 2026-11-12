"""Shared host-profile privacy checks for colleague backends and runtimes.

These are trusted host checks, not a model-facing authorization layer. They
raise ``ValueError`` with the caller's stable error code so each subsystem
keeps its precise failure vocabulary while sharing one implementation.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Union

PathLike = Union[str, Path]


def require_private_directory(path: PathLike, code: str, *, follow_symlinks: bool = False) -> None:
    """Require an owner-only directory (no group/other permission bits).

    ``follow_symlinks=False`` uses ``lstat``: a symlinked profile home must be
    rejected by callers that treat the path itself as the boundary.
    """
    path = Path(path)
    info = path.stat() if follow_symlinks else path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise ValueError(code)


def assert_no_messaging_channels(home: PathLike) -> None:
    """Reject a profile whose config enables any platform or carries IM credentials."""
    home = Path(home)
    try:
        from ruamel.yaml import YAML
        config = YAML(typ="safe").load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except ModuleNotFoundError:
        import yaml
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("profile_config_invalid")
    channels = config.get("platforms") or {}
    if not isinstance(channels, dict) or any(
            not isinstance(v, dict) or v.get("enabled") is True for v in channels.values()):
        raise ValueError("messaging_channels_not_disabled")
    env = home / ".env"
    if env.exists():
        from dotenv import dotenv_values
        names = dotenv_values(env)
        prefixes = ("FEISHU_", "LARK_", "TELEGRAM_", "DISCORD_", "SLACK_", "WHATSAPP_")
        if any(k.upper().startswith(prefixes) and v for k, v in names.items()):
            raise ValueError("messaging_credentials_not_allowed")
