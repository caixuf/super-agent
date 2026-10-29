"""Capability-floor guard: patrol result -> durable episode -> at most one alert.

Detection and repair stay in the host's authoritative inspector
(``ops_patrol.inspect_capability_baseline``). This module never measures
capabilities, edits configuration, or contacts a messaging service. It only

* converts an already-computed patrol result into the outbox inspection shape,
* renders one human message for a recorded drift event, and
* drives the durable outbox so each drift episode produces exactly one event,
  retried while a sender has not confirmed delivery.

A repair that was written to disk is reported as ``config_persisted`` and is
*not* claimed to be live: the running gateway only picks it up after a restart.
"""
from __future__ import annotations

from typing import Any

from agentcore.ops_alerts import AlertOutbox, Sender, deliver_once


def _list_field(patrol: dict[str, Any], name: str) -> list[Any]:
    value = patrol.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"patrol result field {name} must be a list")
    return list(value)


def inspection_from_patrol(patrol: dict[str, Any]) -> dict[str, Any]:
    """Translate one authoritative patrol result into the outbox inspection shape.

    ``drifted`` is true for any observed floor violation, including drift the
    inspector could not repair (``alerts``) and an explicit ``ok is False``
    failure: an inspector that failed must never look healthy here.

    ``config_persisted`` means the inspector wrote repaired values to disk
    during this observation. ``runtime_loaded`` is conservative: as long as
    drift exists, the running gateway is not known to hold the baseline, so it
    stays false even after a successful repair (which needs a restart).
    """
    if not isinstance(patrol, dict):
        raise ValueError("patrol result must be a mapping")

    alerts = _list_field(patrol, "alerts")
    healed = _list_field(patrol, "healed")
    observed = _list_field(patrol, "observed")
    baselines = patrol.get("baselines", {})
    if not isinstance(baselines, dict):
        raise ValueError("patrol result field baselines must be a mapping")

    drifted = bool(alerts) or bool(healed) or patrol.get("ok") is False
    return {
        "drifted": drifted,
        "expected": dict(baselines),
        "observed": observed,
        "config_persisted": bool(healed),
        "runtime_loaded": not drifted,
    }


def _value_text(value: Any) -> str:
    if value is None:
        return "未配置"
    return str(value)


def format_alert(event: dict[str, Any]) -> str:
    """Render one recorded drift event as the human alert text.

    Pure and deterministic: the caller decides where (if anywhere) to send it.
    Values that would not have been observed are reported as unconfigured
    rather than silently omitted, and a disk repair is never described as live.
    """
    if not isinstance(event, dict):
        raise ValueError("alert event must be a mapping")
    if event.get("kind") != "capability_drift":
        raise ValueError("alert event kind must be capability_drift")
    expected = event.get("expected")
    observed = event.get("observed")
    if not isinstance(expected, dict) or not isinstance(observed, list):
        raise ValueError("alert event must carry expected baselines and observed values")

    header = "【能力地板告警】%s/%s 能力参数低于基线" % (
        event.get("profile") or "?", event.get("scope") or "?")
    generation = event.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool) and generation > 0:
        header += f"（第 {generation} 次漂移）"
    lines = [header]

    seen: set[str] = set()
    for entry in observed:
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        key = str(entry["key"])
        seen.add(key)
        floor = entry.get("floor", expected.get(key))
        value = entry.get("value")
        if value is None:
            lines.append(f"· {key}：未配置（基线 {_value_text(floor)}）")
        else:
            lines.append(f"· {key}：实测 {_value_text(value)}，基线 {_value_text(floor)}")
    for key, floor in expected.items():
        if key not in seen:
            lines.append(f"· {key}：无实测值（基线 {_value_text(floor)}）")
    if len(lines) == 1:
        lines.append("· 巡检未提供实测值")

    if event.get("config_persisted") is True:
        lines.append("修复：配置已写入磁盘，重启网关后生效")
    else:
        lines.append("修复：配置未写入（需人工处理）")
    if event.get("runtime_loaded") is True:
        lines.append("运行时：已加载基线")
    else:
        lines.append("运行时：未确认加载基线")
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        lines.append(f"event_id：{event_id}")
    return "\n".join(lines)


def run_guard(*, outbox: AlertOutbox, patrol: dict[str, Any], profile: str,
              scope: str, sender: Sender | None = None,
              lease: float = 60.0) -> dict[str, Any]:
    """Record one patrol observation and deliver at most one pending alert.

    The outbox owns episode deduplication, so a repeated drift observation
    returns the same event and sends nothing new; a healthy observation closes
    the episode so the next drift gets a new event and generation. Pending
    events are retried on later cycles, including a failed send or an
    unconfigured sender.

    ``status`` is ``delivered`` when a message was confirmed this cycle,
    ``pending`` when an alert remains undelivered, ``deduplicated`` when the
    drift was already alerted and nothing is outstanding, and ``quiet`` when
    there is no drift and no backlog.
    """
    inspection = inspection_from_patrol(patrol)
    episode = outbox.observe(
        profile, scope,
        {"drifted": inspection["drifted"],
         "expected": inspection["expected"],
         "observed": inspection["observed"]},
        config_persisted=inspection["config_persisted"],
        runtime_loaded=inspection["runtime_loaded"],
    )
    delivery = deliver_once(outbox, sender, lease=lease)
    pending_count = len(outbox.pending())

    if delivery["status"] == "delivered":
        status = "delivered"
    elif pending_count:
        status = "pending"
    elif episode["drifted"]:
        status = "deduplicated"
    else:
        status = "quiet"

    event = episode.get("event")
    return {
        "status": status,
        "episode": episode,
        "delivery": delivery,
        "message": format_alert(event) if event else None,
        "pending_count": pending_count,
    }
