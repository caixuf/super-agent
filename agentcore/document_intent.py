"""Explainable delivery advice, combining user intent and planned answer structure.

This is a conservative advisor, not a complete semantic classifier. It abstains
when a short request does not reveal whether a visual will improve comprehension.
The caller's model can supply planned_structure after understanding the task.
"""
from __future__ import annotations

import re


def _match(pattern, text):
    return re.search(pattern, text, re.I) is not None


def _count(structure, name):
    value = structure.get(name, 0)
    if isinstance(value, (list, tuple)):
        return len(value)
    return min(value, 100) if type(value) is int and value > 0 else 0


def _result(mode, kind, reason, *, local=False):
    visuals = {'architecture': 'overview and responsibility boundaries',
               'explanation': 'sequence, state transition or causal chain',
               'plan': 'phases, dependencies, deliverables and acceptance',
               'comparison': 'decision matrix and tradeoffs',
               'document': 'a visual only where it explains structure'}
    return {'mode': mode, 'kind': kind, 'reason_code': reason,
            'requires_model_judgment': mode == 'defer',
            'avoid_full_regeneration': local,
            'suggested_visual': visuals.get(kind, 'none')}


def advise_delivery(user_message, *, planned_structure=None, recent_messages=None):
    """Return artifact/inline/defer; explicit user format preferences take priority.

    planned_structure accepts roles, relationships, phases, steps, alternatives
    as nonnegative counts or lists, and reusable/shared as booleans. It describes
    the answer the model intends to give, not the number of words in the prompt.
    recent_messages is reserved for callers that preserve topic context; raw
    history is never inserted into the advice returned to the model.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return _result('defer', 'document', 'insufficient_intent')
    text = re.sub(r'```[\s\S]*?```', '', user_message).strip()[:12000]
    structure = planned_structure if isinstance(planned_structure, dict) else {}
    # A request to avoid text-only output is a positive visual preference.
    positive = _match(r'不要只给文字|别只给文字|不要纯文字|not just text', text)
    if _match(r'(?:生成|导出|做成|create|export|make).{0,30}(?:pptx?|powerpoint|slides?|pdf|docx|word文档)', text):
        return _result('defer', 'document', 'requested_format_needs_appropriate_tool')
    if not positive and _match(r'不要图|不用图|不需要图|纯文字|只用文字|文字就行|文字即可|只要文字|只给文字|一句话|简短回答|text[- ]only|plain text|no diagrams?|one sentence|brief answer', text):
        return _result('inline', 'document', 'explicit_inline_preference')
    regenerate = _match(r'重画|重新生成|更新文档|修改文档|整篇|完整文档|regenerate|update (?:the )?document|whole document|entire document', text)
    if not regenerate and _match(r'这一步|这一行|这个箭头|这个词|第二步|第\d+步|this step|that line|this arrow|this term', text):
        return _result('inline', 'explanation', 'local_followup', local=True)
    bounded = _match(r'完成了吗|现在状态|当前状态|现在怎么样|进度如何|current status|is .{0,30}(?:done|finished)|how many|which command|what (?:is|s) .{0,30}(?:port|version)|(?:端口|版本号|文件路径)(?:是多少|是什么|在哪)|只给.{0,10}命令', text)
    # A report is not a one-line status query: it is a recurring deliverable
    # that people forward, archive, or reopen. Keep direct status questions
    # inline, but treat an explicit report request as an illustrated document.
    if not bounded and _match(r'汇报|报告|周报|日报|进度报告|状态报告|status report|progress report|weekly report|daily report', text):
        return _result('artifact', 'plan', 'progress_report_intent')
    if bounded:
        return _result('inline', 'document', 'bounded_fact_or_status')
    if _match(r'是什么意思|定义一下|一句定义|\bdefine\b|meaning of|what does .{0,50} mean', text):
        return _result('inline', 'explanation', 'term_definition')
    if _match(r'^(什么是|什么叫|what is\b)', text) and not _match(r'机制|流程|架构|系统|方案|服务|pipeline|architecture|process|our |系统如何', text):
        return _result('inline', 'explanation', 'term_definition')

    phases, choices = _count(structure, 'phases'), _count(structure, 'alternatives')
    roles, links, steps = (_count(structure, key) for key in ('roles', 'relationships', 'steps'))
    if phases >= 2:
        return _result('artifact', 'plan', 'planned_phases_and_acceptance')
    if choices >= 2 and (structure.get('shared') is True or _count(structure, 'criteria') >= 2):
        return _result('artifact', 'comparison', 'planned_tradeoff_structure')
    if roles >= 2 and links >= 1:
        return _result('artifact', 'architecture', 'planned_roles_and_relationships')
    if steps >= 3 and (links >= 1 or structure.get('reusable') is True):
        return _result('artifact', 'explanation', 'planned_process_structure')

    if _match(r'路线图|排期|里程碑|分阶段|(?:安排.{0,10}(?:未来|后面).{0,10}周)|后面.{0,10}周.{0,10}安排|未来.{0,10}周.{0,10}安排|实施计划|推进计划|行动计划|roadmap|implementation plan|rollout plan|milestones|next .{0,15}weeks', text):
        return _result('artifact', 'plan', 'phased_action_intent')
    if _match(r'计划|planning|make a plan|project plan', text) and _match(r'帮|制定|规划|安排|做|接下来|怎么|阶段|依赖|风险|验收|create|prepare|plan|next', text):
        return _result('artifact', 'plan', 'phased_action_intent')
    if _match(r'架构|模块边界|谁调用谁|组件关系|数据流|调用链|architecture|system design|component relationships?|data flow', text):
        return _result('artifact', 'architecture', 'structural_explanation_intent')
    if _match(r'比较|对比|取舍|选型|compare|comparison|tradeoffs?|versus', text) and _match(r'方案|设计|技术|选择|优缺点|哪种|which|design|approach|option|solution|tradeoff', text):
        return _result('artifact', 'comparison', 'decision_support_intent')
    if _match(r'讲解|解释|介绍|理解|教我|how .{0,40}works|walk me through|explain|teach|understand', text) and _match(r'机制|过程|流程|工作原理|协作|交互|生命周期|状态机|恢复|pipeline|lifecycle|mechanism|workflow|handoff|recovery|state machine|coordination|interaction', text):
        return _result('artifact', 'explanation', 'process_explanation_intent')
    if regenerate or positive or _match(r'图文|画.{0,8}图|给.{0,8}图|生成文档|做成文档|diagram|visuali[sz]e|illustrated document', text):
        return _result('artifact', 'document', 'explicit_document_or_visual_request')
    return _result('defer', 'document', 'judge_visual_benefit_from_answer_structure')


def delivery_context(advice):
    """Short per-turn guidance; no raw input, history, or executable commands."""
    mode = advice['mode']
    common = ('Use meaning and the planned answer structure, not keyword count, length, or the mere presence of a table. '
              'Respect the user format preference. Lead with the answer and explain who does what in familiar words; '
              'explain necessary terms on first use. Include file names, APIs and internal identifiers only when they help verification. '
              'A visual must improve understanding, not decorate a long answer. '
              'Never claim an artifact exists before successful rendering and visual inspection.')
    if mode == 'inline':
        return ('Delivery advice: answer this bounded request directly in text. '
                'For a local follow-up, explain the relevant part without regenerating the whole document. ' + common)
    if advice.get('reason_code') == 'progress_report_intent':
        return ('Delivery advice: deliver this as an illustrated progress report, not a chat-only audit. '
                'Lead with the overall status in one line, then a per-item table (item / what is done / '
                'what is verified / what is blocked / next owner and date). Keep internal gate vocabulary '
                '(for example sidecar files, Gate0, RP/TP) out of the main table; explain any necessary '
                'term on first use or move it to an appendix. State which snapshot the numbers come from. '
                'Use the existing illustrated-doc-rendering skill and tools/arch_doc.py, then deliver the '
                'document with md_deliver; keep the chat message to a short summary plus the link. ' + common)
    if mode == 'artifact':
        return ('Delivery advice: an illustrated ' + advice['kind'] + ' is likely useful. '
                'Read and apply the existing illustrated-doc-rendering skill; use the existing tools/arch_doc.py renderer. '
                'Choose a visual that explains ' + advice['suggested_visual'] + '. '
                'Lead with the answer, produce a readable preview plus complete HTML, verify rendering, and inspect the result. ' + common)
    return ('Delivery advice: decide whether relationships, stages, dependencies, or tradeoffs in your planned answer '
            'would be clearer in a reusable visual document. If useful, apply illustrated-doc-rendering and the existing renderer; '
            'otherwise answer inline. Use the appropriate artifact tool for an explicitly requested file format. ' + common)


# Stable public aliases for host integrations that use decision-oriented naming.
select_delivery = advise_delivery
choose_delivery = advise_delivery
