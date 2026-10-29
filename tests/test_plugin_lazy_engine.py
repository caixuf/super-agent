"""Plugins that ship callable helpers must not need `agentcore` at import time.

A Hermes runtime installs plugin directories by copy, without the engine package on its
path. An import-time `from agentcore...` makes the plugin fail to load on every process
start; the checkout is deployment configuration handed over in ``register(ctx)``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from agentcore.ops_alerts import AlertOutbox

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "capability_alert_outbox" / "__init__.py"


def _load_without_engine(monkeypatch):
    monkeypatch.setitem(sys.modules, "agentcore", None)  # import agentcore -> ModuleNotFoundError
    spec = importlib.util.spec_from_file_location("capability_alert_outbox_lazy", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_does_not_touch_the_engine(monkeypatch):
    module = _load_without_engine(monkeypatch)
    assert callable(module.create_outbox)


class _Ctx:
    def __init__(self, root):
        self.root = root

    def get_config(self, key, default=None):
        return self.root if key == "engine_root" else default


def test_register_points_helpers_at_the_configured_checkout(monkeypatch, tmp_path):
    module = _load_without_engine(monkeypatch)
    module.register(_Ctx(str(REPO)))          # administrator-supplied checkout
    outbox = module.create_outbox(str(tmp_path / "queue"))
    assert isinstance(outbox, AlertOutbox)
    result = module.deliver_pending(outbox, None)
    assert result.get("status") == "unavailable"  # no transport is a reported state, not an exception


def test_helpers_without_engine_root_fail_loudly(monkeypatch, tmp_path):
    module = _load_without_engine(monkeypatch)
    module._ENGINE["root"] = ""                      # no checkout configured
    monkeypatch.setitem(sys.modules, "agentcore.ops_alerts", None)
    with pytest.raises((RuntimeError, ModuleNotFoundError)):
        module.create_outbox(str(tmp_path / "queue"))
