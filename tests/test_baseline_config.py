"""Unit tests for agentcore.baseline_config (Super Agent baseline contract & enforcement)."""
from __future__ import annotations

import yaml
from pathlib import Path

from agentcore.baseline_config import (
    CORE_RUNTIME_BASELINE,
    audit_config_dict,
    enforce_baseline_config,
)


def test_audit_config_empty_reports_deviations():
    empty_cfg = {}
    deviations = audit_config_dict(empty_cfg)
    assert len(deviations) > 0
    paths = [d.key_path for d in deviations]
    assert "model.default" in paths
    assert "streaming.transport" in paths
    assert "display.busy_input_mode" in paths
    assert "platforms.feishu.card_replies" in paths
    assert "plugins.entries.illustrated-delivery.settings.enforce_delivery" in paths


def test_audit_config_matching_baseline_reports_clean():
    clean_cfg = {
        "model": {
            "default": "flash",
        },
        "streaming": {
            "enabled": True,
            "transport": "edit",
            "buffer_threshold": 120,
            "edit_interval": 0.8,
        },
        "display": {
            "busy_ack_enabled": True,
            "busy_input_mode": "steer",
            "platforms": {
                "feishu": {
                    "streaming": True,
                    "busy_ack_detail": True,
                    "cleanup_progress": True,
                    "tool_progress": False,
                    "thinking_progress": False,
                }
            },
        },
        "platforms": {
            "feishu": {
                "enabled": True,
                "card_replies": True,
                "exec_approval_text": True,
                "home_channel": "oc_123",  # Private fields preserved
            }
        },
        "plugins": {
            "entries": {
                "illustrated-delivery": {
                    "settings": {
                        "enforce_delivery": False,
                    }
                }
            }
        },
    }
    deviations = audit_config_dict(clean_cfg)
    assert len(deviations) == 0


def test_enforce_baseline_config_idempotent_repair(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    # Legacy/drifted config missing critical streaming transport and delivery flag
    initial_data = {
        "streaming": {
            "enabled": True,
            "edit_interval": 0.8,
        },
        "display": {
            "busy_input_mode": "queue",  # Wrong mode
        },
        "platforms": {
            "feishu": {
                "enabled": True,
                "home_channel": "oc_private_channel",
            }
        },
    }
    cfg_file.write_text(yaml.dump(initial_data), encoding="utf-8")

    # First enforcement: repairs deviations
    modified, changes = enforce_baseline_config(cfg_file, backup=True)
    assert modified is True
    assert len(changes) >= 3

    repaired = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert repaired["model"]["default"] == "flash"
    assert repaired["streaming"]["transport"] == "edit"
    assert repaired["display"]["busy_input_mode"] == "steer"
    assert repaired["platforms"]["feishu"]["card_replies"] is True
    assert repaired["platforms"]["feishu"]["home_channel"] == "oc_private_channel"
    assert repaired["plugins"]["entries"]["illustrated-delivery"]["settings"]["enforce_delivery"] is False

    # Second enforcement: already clean, no changes applied
    modified_again, changes_again = enforce_baseline_config(cfg_file, backup=True)
    assert modified_again is False
    assert changes_again == []

