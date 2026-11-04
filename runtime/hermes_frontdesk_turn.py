"""End a frontdesk turn from fresh durable-submit receipts, without another model.

Installed as superagent_frontdesk_turn.py beside the host policy helper. The host
supplies only this round's submit IDs and newly appended canonical tool results.
This module neither submits tasks nor sends messages.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

UNCONFIRMED = (
    "后台提交结果未确认，前台本轮已结束。请保留原请求键并查询核对，"
    "不要换键重复提交。"
)
UNAVAILABLE = (
    "frontdesk_handoff_unavailable: 前台交接检查不可用，本轮停止。"
    "提交结果可能已持久化；请保留原请求键核对，不要换键重复提交。"
)
_STATES = {
    "queued": "排队中", "running": "执行中", "waiting_user": "等待用户",
    "awaiting_plan_approval": "等待计划批准", "completed": "主账完成",
    "failed": "主账失败", "cancelled": "主账取消",
}


def finalize_after_release(agent: Any) -> bool:
    """Close the display stream box as soon as a durable handoff ends the turn.

    The host's guardrail-halt and pre-tool-flush paths emit an EOS to
    ``stream_delta_callback`` so an open progress card is settled immediately.
    A frontdesk handoff must do the same; otherwise the box lingers until turn
    teardown. This never sends a message, never touches the task ledger, and
    never raises into the model loop. Returns whether an EOS was emitted.
    """
    callback = getattr(agent, "stream_delta_callback", None)
    if not callable(callback):
        return False
    try:
        callback(None)
    except Exception:
        return False
    return True


def release_after_submission(
    submit_call_ids: list[str], tool_results: list[dict[str, Any]],
    *, home: Path | str | None = None,
) -> str | None:
    """Return a foreground acknowledgement or None for an ordinary tool round.

    Policy failures propagate to the host's independent fail-closed boundary.
    No user/model text, prior history, status result, summary or next_action is
    interpreted as a successful submission receipt.
    """
    if not submit_call_ids:
        return None
    from superagent_frontdesk import frontdesk_enabled

    if not frontdesk_enabled(home=home):
        return None
    if len(submit_call_ids) > 64 or len(tool_results) > 256:
        return UNCONFIRMED
    by_id: dict[str, list] = {call_id: [] for call_id in submit_call_ids}
    for row in tool_results:
        if (isinstance(row, dict) and row.get("role") == "tool"
                and row.get("name") == "durable_submit"
                and isinstance(row.get("tool_call_id"), str)
                and row["tool_call_id"] in by_id):
            by_id[row["tool_call_id"]].append(row.get("content"))
    accepted: dict[str, tuple[str, str | None]] = {}
    uncertain = 0
    for call_id in submit_call_ids:
        values = by_id[call_id]
        receipt = None
        if len(values) == 1 and isinstance(values[0], str) and len(values[0]) <= 65536:
            try:
                receipt = json.loads(values[0])
            except (ValueError, RecursionError):
                pass
        if (not isinstance(receipt, dict) or receipt.get("ok") is not True
                or not isinstance(receipt.get("task_id"), str)
                or re.fullmatch("[0-9a-f]{32}", receipt["task_id"]) is None
                or not isinstance(receipt.get("status"), str)
                or receipt["status"] not in _STATES):
            uncertain += 1
            continue
        role = receipt.get("role")
        if not isinstance(role, str) or re.fullmatch("[a-z][a-z0-9_]{0,31}", role) is None:
            role = None
        accepted[receipt["task_id"]] = (receipt["status"], role)
    if not accepted:
        return UNCONFIRMED
    lines = ["已持久记录后台任务：", ""]
    for task_id, (state, role) in accepted.items():
        lines.append(f"- `{task_id}`：{_STATES[state]}" + (f"（`{role}`）" if role else ""))
    lines.extend(["", "前台回合已结束，可继续发消息；后台结果可按任务 ID 查询。"
                  "接单回执不等于验收或交付完成。"])
    if uncertain:
        lines.append(f"另有 {uncertain} 笔提交结果未确认；保留原请求键核对，不要换键重复提交。")
    return "\n".join(lines)
