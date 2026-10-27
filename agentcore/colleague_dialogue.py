"""Present durable workflow results with host-verified colleague attribution.

This is a transport decorator for WorkflowDelivery, not a platform client. Its
binding resolver is privileged infrastructure, never a tool or model input:
it must read durable dispatch records proving that the exact child job was
executed by that member generation. A role, model output or session id alone is
not proof. This module does not create or authenticate those records.

The platform client must display ``payload['text']`` as literal plain text, not
interpret model output as rich text, mentions, or platform card markup. It is
the primary bot's platform account that forwards this content. Receipt handling,
owner checks, single-sender coordination and deduplication stay in
WorkflowDelivery; this decorator is not an independent sending entry point.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from agentcore.colleague_registry import Registry, Supervisor

_WORKFLOW = re.compile(r'[0-9a-f]{32}\Z')
_ROLE = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')
_STATES = {'completed': '已完成', 'failed': '未完成', 'cancelled': '已取消',
           'running': '处理中', 'queued': '等待处理'}


def _clean(value, maximum=4096):
    """Remove invisible controls and keep each untrusted line visibly quoted."""
    if not isinstance(value, str):
        return ''
    value = value[:maximum]
    result = []
    for char in value:
        if char in '\n\t':
            result.append(char)
        elif not unicodedata.category(char).startswith('C'):
            result.append(char)
    return ''.join(result).strip()


def _name(value):
    # A member can choose a name containing punctuation used by our wrapper.
    # Keep its stable ASCII id alongside it, and prevent wrapper impersonation.
    return (' '.join(_clean(value, 80).splitlines()).replace('\t', ' ')
            .replace('【', '［').replace('】', '］').replace('｜', '¦'))


class DialogueTransport:
    """Render a claimed workflow result and forward it through a trusted client.

    ``resolve_binding(workflow, ordinal, child_id)`` returns either None or
    exactly ``{'member_id': id, 'generation': int}`` from host-owned durable
    execution binding. The resolver must verify the actual executor, not just
    who was assigned. Failures, stale generations and role mismatch are shown
    as unknown. The registry is not a cryptographic boundary against its host.
    """

    def __init__(self, registry, supervisor, owner, resolve_binding, transport):
        if (not isinstance(registry, Registry) or type(supervisor) is not Supervisor
                or supervisor.profile_id != registry.profile_id):
            raise PermissionError('trusted registry supervisor required')
        if not callable(resolve_binding) or not callable(getattr(transport, 'send', None)) \
                or not callable(getattr(transport, 'lookup', None)):
            raise ValueError('trusted binding resolver and transport required')
        if (not isinstance(owner, dict) or set(owner) != {'profile', 'session'}
                or any(not isinstance(item, str) or not item.strip() for item in owner.values())):
            raise PermissionError('authenticated workflow owner required')
        self.registry, self.supervisor = registry, supervisor
        self.owner, self.resolve_binding, self.transport = deepcopy(owner), resolve_binding, transport

    def _check_owner(self, origin):
        if (not isinstance(origin, dict) or origin.get('profile') != self.owner['profile']
                or origin.get('session') != self.owner['session']):
            raise PermissionError('workflow owner mismatch')

    def _speaker(self, workflow, stage):
        child = stage['child_id']
        if child is None:
            return '执行同事未确认'
        try:
            binding = self.resolve_binding(workflow, stage['ordinal'], child)
            if (not isinstance(binding, dict) or set(binding) != {'member_id', 'generation'}
                    or type(binding['generation']) is not int or binding['generation'] < 1):
                return '执行同事未确认'
            member = self.registry.role_for_member(supervisor=self.supervisor,
                         member_id=binding['member_id'], generation=binding['generation'])
            if member['role_id'] != stage['role']:
                return '执行同事未确认'
            if binding['member_id'] == self.registry.primary_member(supervisor=self.supervisor):
                return '主 Bot 自己处理'
            return ('同事：' + _name(member['display_name']) + '｜成员 ID：' + member['member_id']
                    + '｜岗位：' + _name(member['label']))
        except Exception:
            # Absence or failure of the trusted binding source is not evidence
            # that any particular member authored this result.
            return '执行同事未确认'

    def render(self, payload):
        """Render only the immutable final record claimed by WorkflowDelivery."""
        if (not isinstance(payload, dict) or not isinstance(payload.get('workflow_id'), str)
                or not _WORKFLOW.fullmatch(payload['workflow_id'])):
            raise ValueError('invalid workflow result')
        workflow, status, stages = payload['workflow_id'], payload.get('status'), payload.get('stages')
        if status not in ('completed', 'failed', 'cancelled') or not isinstance(stages, list) or not 1 <= len(stages) <= 4:
            raise ValueError('invalid workflow result')
        lines = ['主 Bot 转交｜任务 ' + workflow + '｜' + _STATES[status]]
        for ordinal, stage in enumerate(stages):
            if (not isinstance(stage, dict) or type(stage.get('ordinal')) is not int
                    or stage['ordinal'] != ordinal or not isinstance(stage.get('role'), str)
                    or not _ROLE.fullmatch(stage['role']) or stage.get('state') not in _STATES
                    or (stage.get('child_id') is not None and
                        (not isinstance(stage['child_id'], str) or not _WORKFLOW.fullmatch(stage['child_id'])))):
                raise ValueError('invalid workflow stage')
            lines.extend(['', '【' + self._speaker(workflow, stage) + '】',
                          '岗位编号：' + stage['role'] + '｜状态：' + _STATES[stage['state']]])
            if stage['state'] == 'completed':
                evidence = stage.get('evidence')
                if not isinstance(evidence, dict) or not isinstance(evidence.get('summary'), str):
                    raise ValueError('invalid workflow evidence')
                body = _clean(evidence['summary']) or '（没有摘要）'
                lines.append('阶段结果（由主 Bot 转交）：')
                lines.extend('│ ' + line for line in body.splitlines())
        return '\n'.join(lines)

    def send(self, *, origin, delivery_key, payload):
        self._check_owner(origin)
        text = self.render(payload)
        # The downstream transport renders ONLY this literal plain text; evidence
        # objects and model-supplied routing data are not passed through.
        outbound = {'workflow_id': payload['workflow_id'], 'status': payload['status'],
                    'origin': deepcopy(origin), 'text': text}
        return self.transport.send(origin=deepcopy(origin), delivery_key=delivery_key, payload=outbound)

    def lookup(self, *, origin, delivery_key):
        self._check_owner(origin)
        return self.transport.lookup(origin=deepcopy(origin), delivery_key=delivery_key)
