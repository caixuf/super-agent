#!/usr/bin/env python3
"""Install explicit profile-opted frontdesk gates in an INACTIVE Hermes tree.

No profile config, services or running processes are changed. All source anchors
are validated and compiled before any write. Missing/drifted anchors refuse the
whole operation. Apply to a candidate, verify behavior, then migrate separately.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

REPO = Path(__file__).resolve().parents[1]
HELPER = Path("superagent_frontdesk.py")
TURN_HELPER = Path("superagent_frontdesk_turn.py")
FAILURE = "frontdesk_policy_unavailable: execution refused; ask the administrator to check this profile."
HANDOFF_FAILURE = (
    "frontdesk_handoff_unavailable: 前台交接检查不可用，本轮停止。"
    "提交结果可能已持久化；请保留原请求键核对，不要换键重复提交。"
)


def _tool_gate(indent, name):
    return (
        f"{indent}# SUPERAGENT_FRONTDESK_TOOL_GATE\n"
        f"{indent}try:\n"
        f"{indent}    from superagent_frontdesk import tool_block_message as _frontdesk_authorize\n"
        f"{indent}    _frontdesk_block = _frontdesk_authorize({name})\n"
        f"{indent}except Exception:\n"
        f"{indent}    _frontdesk_block = {FAILURE!r}\n"
    )


_FILTER = '''
def _superagent_frontdesk_filter(definitions):
    # SUPERAGENT_FRONTDESK_SCHEMA_GATE
    try:
        from superagent_frontdesk import filter_tool_definitions
        return filter_tool_definitions(definitions)
    except Exception:
        return []


'''
_DIRECT_ANCHOR = "    # Tool Search bridge: tool_search / tool_describe are catalog reads handled\n"
_DIRECT_GATE = _tool_gate("    ", "function_name") + (
    "    if _frontdesk_block is not None:\n"
    '        return _emit(json.dumps({"error": _frontdesk_block}), status="blocked",\n'
    '                     error_type="frontdesk_policy", error_message=_frontdesk_block)\n\n'
)
_EXEC_ANCHOR = '    block_message, block_error_type = scope_block, "tool_scope_block"\n'
_EXEC_GATE = _tool_gate("    ", "ref.name") + (
    '    block_message = scope_block or _frontdesk_block\n'
    '    block_error_type = "frontdesk_policy" if _frontdesk_block else "tool_scope_block"\n'
)
_INVOKE_ANCHOR = "    block_message: Optional[str] = None\n    if not pre_tool_block_checked:\n"
_INVOKE_GATE = _tool_gate("    ", "function_name") + (
    "    block_message: Optional[str] = _frontdesk_block\n"
    "    if block_message is None and not pre_tool_block_checked:\n"
)
_INBOUND_ANCHOR = "        event, source, is_internal = _admitted\n"
_INBOUND_GATE = '''        # SUPERAGENT_FRONTDESK_COMMAND_GATE
        try:
            from superagent_frontdesk import command_block_message as _frontdesk_command
            _frontdesk_block = _frontdesk_command(
                event.get_command(), home=self._resolve_profile_home_for_source(source))
        except Exception:
            _frontdesk_block = ''' + repr(FAILURE) + '''
        if _frontdesk_block is not None:
            return _frontdesk_block
'''
_ROUND_EXECUTE = "    agent._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)\n"
_ROUND_START = (
    "    # SUPERAGENT_FRONTDESK_RESULT_BOUNDARY\n"
    "    _frontdesk_result_start = len(messages)\n" + _ROUND_EXECUTE
)
_ROUND_END = "    # Reset per-turn retry counters so one truncation can't poison the turn.\n"
_ROUND_RELEASE = '''    # SUPERAGENT_FRONTDESK_TURN_RELEASE
    _frontdesk_submit_ids = [coalesce_tool_call_id(tc) for tc in assistant_message.tool_calls
                             if tc.function.name == "durable_submit"]
    _frontdesk_reply = None
    if _frontdesk_submit_ids:
        try:
            from superagent_frontdesk_turn import release_after_submission
            _frontdesk_reply = release_after_submission(
                _frontdesk_submit_ids, messages[_frontdesk_result_start:])
        except Exception:
            _frontdesk_reply = ''' + repr(HANDOFF_FAILURE) + '''
            failed = True
    if _frontdesk_reply is not None:
        final_response = _frontdesk_reply
        _turn_exit_reason = "frontdesk_handoff"
        append_message(messages, {"role": "assistant", "content": final_response})
        agent._session_messages = messages
        return _verdict("break")

'''
_ROUND_COMPLETED = 'or str(_turn_exit_reason).startswith("text_response(")'
_REVIEW_GATE = _tool_gate("        ", '"memory"') + (
    "        if _frontdesk_block is not None:\n"
    "            return\n"
)
_REVIEW_START = "        # Gates run at enqueue/spawn time; the idle dispatcher re-checks `enabled` at dispatch time.\n"
_REVIEW_RESUME = "        from agent.background_review import (\n            finish_background_review_run, prepare_background_review_run, spawn_background_review_thread,\n        )\n"

EDITS = {
    Path("model_tools.py"): (
        ("def get_tool_definitions(", _FILTER + "def get_tool_definitions("),
        ("        return compute()\n", "        return _superagent_frontdesk_filter(compute())\n"),
        ("            return list(result)\n", "            return _superagent_frontdesk_filter(list(result))\n"),
        ("    return list(cached)\n", "    return _superagent_frontdesk_filter(list(cached))\n"),
        (_DIRECT_ANCHOR, _DIRECT_GATE + _DIRECT_ANCHOR),
    ),
    Path("agent/tool_executor.py"): ((_EXEC_ANCHOR, _EXEC_GATE),),
    Path("agent/agent_runtime_helpers.py"): ((_INVOKE_ANCHOR, _INVOKE_GATE),),
    Path("gateway/run_inbound.py"): ((_INBOUND_ANCHOR, _INBOUND_ANCHOR + _INBOUND_GATE),),
    Path("agent/turn_tool_round.py"): (
        (_ROUND_EXECUTE, _ROUND_START), (_ROUND_END, _ROUND_RELEASE + _ROUND_END),
    ),
    Path("agent/turn_finalizer.py"): (
        (_ROUND_COMPLETED, _ROUND_COMPLETED + ' or _turn_exit_reason == "frontdesk_handoff"'),
    ),
    Path("run_agent.py"): (
        (_REVIEW_START, _REVIEW_GATE + _REVIEW_START),
        (_REVIEW_RESUME, _REVIEW_GATE + _REVIEW_RESUME),
    ),
}


def transform(relative: Path, source: str) -> str:
    after = source
    for old, new in EDITS[relative]:
        if new in after:
            if after.count(new) != 1:
                raise ValueError("duplicate frontdesk gate")
            continue
        if after.count(old) != 1:
            raise ValueError("frontdesk source anchor drifted: " + relative.as_posix())
        after = after.replace(old, new, 1)
    compile(after, relative.as_posix(), "exec")
    return after


def _write_atomic(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("refusing symlink patch target")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, name = tempfile.mkstemp(prefix=".frontdesk-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.agent_dir.resolve(strict=True)
        changes = {}
        for relative in EDITS:
            target = root / relative
            if target.resolve(strict=True) != target or not target.is_file():
                raise ValueError("frontdesk patch requires regular source files")
            source = target.read_text(encoding="utf-8")
            after = transform(relative, source)
            if after != source:
                changes[target] = after
        destinations = set()
        for relative, source_name in (
                (HELPER, "hermes_frontdesk_policy.py"), (TURN_HELPER, "hermes_frontdesk_turn.py")):
            helper = (REPO / "runtime" / source_name).read_text(encoding="utf-8")
            compile(helper, relative.as_posix(), "exec")
            destination = root / relative
            destinations.add(destination)
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ValueError("refusing nonregular policy helper")
            if not destination.is_file() or destination.read_text(encoding="utf-8") != helper:
                changes[destination] = helper
        if args.check:
            print("frontdesk boundary " + ("needs installation" if changes else "installed"))
            return 1 if changes else 0
        # Helper first: a process accidentally importing partially updated code
        # still refuses unsafe calls. This is NOT a hot-reload/deployment command.
        for path in sorted(changes, key=lambda path: path not in destinations):
            _write_atomic(path, changes[path])
        print("frontdesk boundary installed; verify candidate before enabling a profile")
        return 0
    except (OSError, ValueError, SyntaxError) as error:
        print("frontdesk boundary refused: " + (
            str(error) if isinstance(error, ValueError) else type(error).__name__))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
