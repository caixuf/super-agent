"""Host bootstrap for a single-principal automatic memory worker.

Paths and identities are administrator-provided environment, not model input.
This module only configures the engine; it does not start a worker or access
an existing store at import time.
"""
from __future__ import annotations

import os
from pathlib import Path

from agentcore.ccp_analysis_model import model as ccp_model
from agentcore.memory.config import MemoryConfig, configure


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value or len(value) > 512 or any(ch in value for ch in "\r\n\0"):
        raise ValueError("missing or invalid " + name)
    return value


def chat(messages, *, timeout=45, max_tokens=800, temperature=0):
    """Use the existing loopback-only CCP adapter; never log provider content."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError("invalid_timeout")
    seconds = max(1.0, min(float(timeout), 300.0))
    return {"content": ccp_model(messages, alias="flash", timeout_seconds=seconds)}


def bootstrap():
    directory = Path(_required("SUPERAGENT_MEMORY_DATA_DIR"))
    user = _required("SUPERAGENT_MEMORY_USER")
    agent = _required("SUPERAGENT_MEMORY_AGENT")
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError("private absolute memory data directory required")
    info = directory.stat()
    if info.st_mode & 0o077 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError("private memory data directory required")
    raw_dim = os.environ.get("SUPERAGENT_MEMORY_EMBED_DIM", "1024")
    try:
        dimension = int(raw_dim)
    except ValueError:
        raise ValueError("invalid embedding dimension") from None
    if not 1 <= dimension <= 8192:
        raise ValueError("invalid embedding dimension")
    endpoint = os.environ.get("SUPERAGENT_MEMORY_EMBED_URL", "").strip()
    embed_fn = None
    if endpoint:
        from agentcore.memory.bge_sidecar import make_embedder
        embed_fn = make_embedder(endpoint, dimension=dimension)
    configure(MemoryConfig(data_dir=directory, default_user=user, default_agent=agent,
                           embed_dim=dimension, embed_fn=embed_fn, llm_fn=chat))
