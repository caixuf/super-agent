"""测试夹具：确定性离线嵌入 + 每次测试独立的临时记忆库。

设计约束（见 README）：
- **绝不依赖网络或内网 ollama**：所有测试都通过 ``embed_fn`` 注入确定性实现。
- **每次测试独立 data_dir**：避免全局注册表/索引跨用例污染。
- **测试结束前汇合后台向量线程**：写入是异步的，否则会丢索引、制造 flaky。
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

import pytest

from agentcore.memory.config import MemoryConfig, configure, reset_config


@pytest.fixture(scope="session", autouse=True)
def _isolate_agentcore_state(tmp_path_factory):
    """Point every implicit ``agentcore`` state path at a session tmp dir.

    ``agentcore.liveness.default_db_path()`` falls back to the **user's real**
    ``~/.local/state/agentcore/liveness.db``. A test that builds a ledger — or a plugin
    service that leaves ``liveness_path`` unset — then writes into the live ledger of
    whatever profile happens to run on this machine: measured on 2026-10-29, 457 of the
    561 rows in the live file carried test owners (``test-profile:*`` / ``session-one``).

    The module honors ``AGENTCORE_LIVENESS_DB`` / ``AGENTCORE_STATE_DIR`` at call time and
    subprocesses inherit this environment, so a session-scoped override covers both
    in-process and real-host cases. ``AGENTCORE_LOCK_DIR`` is redirected for the same
    reason (dev-colleague worktree locks default to a shared ``/tmp`` path).
    """
    root = tmp_path_factory.mktemp("agentcore-state")
    overrides = {
        "AGENTCORE_STATE_DIR": str(root),
        "AGENTCORE_LIVENESS_DB": str(root / "liveness.db"),
        "AGENTCORE_LOCK_DIR": str(root / "locks"),
    }
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield root
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def deterministic_embed(texts: Sequence[str], dim: int | None = None):
    """确定性离线嵌入：字符 n-gram 哈希投影 + L2 归一化。

    - 完全相同输入 -> 完全相同输出（可复现，无网络）。
    - 共享词/n-gram 的文本余弦更高（足以让「写入 -> 召回」闭环成立）。
    - 维度由调用方显式给定，避免依赖全局配置。
    """
    if dim is None:
        dim = 64
    out = []
    for t in texts:
        v = [0.0] * dim
        s = (t or "").lower()
        grams = list(re.findall(r"[a-z0-9_]+", s))
        for run in re.findall(r"[\u4e00-\u9fff]+", s):
            grams += [run[i] for i in range(len(run))]
            grams += [run[i:i + 2] for i in range(len(run) - 1)]
        for g in grams:
            h = hashlib.sha256(g.encode("utf-8")).digest()
            v[int.from_bytes(h[:8], "big") % dim] += 1.0 if h[8] & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v] if n else v)
    return out


def make_config(data_dir, **overrides) -> MemoryConfig:
    kwargs = dict(
        data_dir=Path(data_dir),
        embed_dim=64,
        embed_fn=lambda texts: deterministic_embed(texts, 64),
        era_domains=("automotive",),
        # 代际「写法」也是宿主约定：夹具统一注入 "G1"/"G2" 这组中性写法，
        # 这样测试数据不必携带任何具体企业的代际名。
        era_pattern=r"\bG(\d+)(?:\.\d+)?\b",
        era_prefix="G",
    )
    kwargs.update(overrides)
    return MemoryConfig(**kwargs)


@contextmanager
def memory_configured(data_dir, **overrides):
    """上下文管理器：安装配置 -> yield memory_store -> 汇合后台线程并复位。"""
    from agentcore.memory import memory_store as ms

    reset_config()
    configure(make_config(data_dir, **overrides))
    try:
        yield ms
    finally:
        ms.flush_background(timeout=30)
        reset_config()


@pytest.fixture
def engine(tmp_path):
    """默认夹具：全新 tmp 目录 + 确定性嵌入，测试后自动汇合与复位。"""
    with memory_configured(tmp_path) as ms:
        yield ms


@pytest.fixture
def configured():
    """返回 ``memory_configured`` 上下文管理器，供需要自定义 data_dir/身份 的用例使用。"""
    return memory_configured


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]