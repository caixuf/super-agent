"""Present a single read-only task from its host-owned persistent record.

This does not send messages or grant access. The host must authenticate the
owner supplied at construction and protect runtime storage. No caller-supplied
text or member identity is accepted. ``present`` reads only through the runtime
and attributes a completed result to the persisted member generation. The task
uses the runtime's configured callable; this module does not establish an
independent process, remote model or platform account for the colleague.
"""
from __future__ import annotations

from copy import deepcopy
import re

from agentcore.colleague_dialogue import _clean, _name
from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime

_TASK = re.compile(r'[0-9a-f]{32}\Z')


class ReadOnlyDialogue:
    """Construct one literal-text relay from the authenticated owner's record.

    The owner is host-bound, never a model-selected tool parameter. A platform
    adapter must use its own authenticated recipient binding, plain-text output,
    durable outbox and explicit receipt. The returned delivery key is only a
    stable identifier; it is not by itself a deduplication mechanism.
    """

    def __init__(self, runtime, *, owner):
        if not isinstance(runtime, ReadOnlyColleagueRuntime):
            raise ValueError('trusted runtime required')
        if (not isinstance(owner, dict) or set(owner) != {'profile', 'session'}
                or any(not isinstance(item, str) or not item.strip() for item in owner.values())):
            raise PermissionError('authenticated owner required')
        self.runtime, self.owner = runtime, deepcopy(owner)

    def present(self, request_id):
        """Read a result; incomplete tasks never contain an attributed reply."""
        record = self.runtime.inspect_result(owner=deepcopy(self.owner), request_id=request_id)
        if record is None:
            return {'state': 'not_found', 'text': None}
        task, state = record.get('task_id'), record.get('state')
        if (not isinstance(task, str) or not _TASK.fullmatch(task)
                or record.get('owner') != self.owner):
            raise PermissionError('result_owner_or_task_mismatch')
        if state != 'completed':
            return {'task_id': task, 'state': state, 'text': None}
        result = record.get('result')
        if (record.get('checked_result') is not True or not isinstance(result, dict)
                or result.get('verification') != 'citation_traceability_only'
                or not isinstance(result.get('summary'), str)
                or not isinstance(result.get('limitations'), list)
                or any(not isinstance(item, str) for item in result['limitations'])):
            raise ValueError('invalid_completed_result')
        member = self.runtime.registry.role_for_member(
            supervisor=self.runtime.supervisor, member_id=record.get('member_id'),
            generation=record.get('generation'))
        if (member['role_id'] != record.get('role_id') or member['member_id'] ==
                self.runtime.registry.primary_member(supervisor=self.runtime.supervisor)):
            raise PermissionError('member_binding_mismatch')
        lines = ['主 Bot 转交｜任务 ' + task + '｜已完成', '',
                 '【同事：' + _name(member['display_name']) + '｜成员 ID：' + member['member_id']
                 + '｜岗位：' + _name(member['label']) + '】',
                 '只读分析结果（由主 Bot 转交）：']
        body = _clean(result['summary'], 2000) or '（没有摘要）'
        lines.extend('│ ' + line for line in body.splitlines())
        if result.get('summary_truncated') is True:
            lines.append('│ （摘要已截断）')
        limitations = [_clean(item, 300) for item in result['limitations'][:3]]
        if any(limitations):
            lines.append('结果的局限：')
            for item in limitations:
                if item:
                    lines.extend('│ ' + line for line in item.splitlines())
        return {'task_id': task, 'state': state, 'delivery_key': 'readonly-final:' + task,
                'sender': 'primary_relay', 'member_id': member['member_id'],
                'generation': member['generation'], 'role_id': member['role_id'],
                'text': '\n'.join(lines)}
