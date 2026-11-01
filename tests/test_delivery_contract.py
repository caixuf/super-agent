"""End-to-end delivery contract for the illustrated-delivery plugin.

These tests deliberately use real user-shaped turns rather than only calling
``advise_delivery``. They pin the seam that failed in production: the advisor
can be right while the final answer still arrives without a document.
"""
import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PATH = REPO / 'plugins/illustrated_delivery/__init__.py'
SPEC = importlib.util.spec_from_file_location('illustrated_delivery_contract_plugin', PATH)
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class Context:
    def __init__(self, **settings):
        self.settings, self.hooks = settings, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def _context(tmp_path):
    context = Context(
        enabled=True,
        engine_root=str(REPO),
        audit_path=str(tmp_path / 'delivery_guard.jsonl'),
    )
    PLUGIN.register(context)
    return context


def _turn(context, message, response, *, session_id='s', turn_id='t'):
    advice = context.hooks['pre_llm_call'](
        user_message=message, session_id=session_id, turn_id=turn_id, platform='api')
    transformed = context.hooks['transform_llm_output'](
        response_text=response, session_id=session_id, model='test', platform='api')
    final = response if transformed is None else transformed
    context.hooks['post_llm_call'](
        session_id=session_id, turn_id=turn_id, user_message=message,
        assistant_response=final, model='test', platform='api')
    return advice, transformed, final


def _records(tmp_path):
    path = tmp_path / 'delivery_guard.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


PROGRESS = '汇报下<business-domain>新需求用例进度，昨天跟进的八个'
EXPLANATION = '讲解一下现在 agent 写<business-domain>新需求用例的流程'
STATUS = '现在进度如何'


def test_progress_report_without_a_document_is_flagged(tmp_path):
    context = _context(tmp_path)
    advice, transformed, final = _turn(
        context, PROGRESS, '八条需求里七条有用例，共 34 条，全部未跑；侧车齐备度不达。')

    assert 'progress report' in advice['context']
    assert transformed is not None
    assert '交付校验未通过' in transformed
    assert '交付校验未通过' in final
    record = _records(tmp_path)[-1]
    assert record['artifact_expected'] is True
    assert record['artifact_present'] is False
    assert record['guard_warned'] is True


def test_progress_report_with_a_document_link_passes(tmp_path):
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '摘要见下。\n\n图文文档：https://example.com/docx/abc123')

    assert transformed is None
    record = _records(tmp_path)[-1]
    assert record['artifact_expected'] is True
    assert record['artifact_present'] is True
    assert record['guard_warned'] is False


def test_file_marker_satisfies_the_contract(tmp_path):
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '报告已生成。\n\n##FILE: /tmp/report.html')

    assert transformed is None
    assert _records(tmp_path)[-1]['artifact_present'] is True


def test_attached_document_satisfies_the_contract(tmp_path):
    """A real document shipped through the media channel is a delivery.

    The chat's ``MEDIA:`` line is how the file actually reaches the reader, so a
    reply that carries it has delivered, even without a ``##FILE:`` marker.
    """
    doc = tmp_path / 'report.html'
    doc.write_text('<html><body>report</body></html>', encoding='utf-8')
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '完整文档见附件。\n\nMEDIA:%s' % doc)

    assert transformed is None
    assert _records(tmp_path)[-1]['artifact_present'] is True


def test_media_line_pointing_nowhere_does_not_count(tmp_path):
    """Naming a file is not sending one; a dead path must still warn."""
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '见附件。\n\nMEDIA:%s' % (tmp_path / 'missing.html'))

    assert transformed is not None
    assert '交付校验未通过' in transformed
    assert _records(tmp_path)[-1]['artifact_present'] is False


def test_a_screenshot_is_not_the_document(tmp_path):
    """A PNG preview travels the same channel but is not the deliverable."""
    shot = tmp_path / 'page-01.png'
    shot.write_bytes(b'\x89PNG\r\n\x1a\n')
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '效果如上。\n\nMEDIA:%s' % shot)

    assert transformed is not None
    assert _records(tmp_path)[-1]['artifact_present'] is False


def test_ordinary_code_url_does_not_count_as_a_delivered_document(tmp_path):
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, PROGRESS, '详见 https://gitlab.example.com/team/repo/-/merge_requests/1')

    assert transformed is not None
    assert '交付校验未通过' in transformed
    assert _records(tmp_path)[-1]['artifact_present'] is False


def test_bounded_status_query_is_not_forced_into_a_document(tmp_path):
    context = _context(tmp_path)
    advice, transformed, _final = _turn(context, STATUS, '当前 8 条里 7 条有用例。')

    assert 'bounded request' in advice['context']
    assert transformed is None
    record = _records(tmp_path)[-1]
    assert record['artifact_expected'] is False
    assert record['guard_warned'] is False


def test_explanation_turn_is_guarded_too(tmp_path):
    context = _context(tmp_path)
    _advice, transformed, _final = _turn(
        context, EXPLANATION, '这条流水线分六段：分诊、评审、逆向、合成、评审、跑测。')

    assert transformed is not None
    assert '交付校验未通过' in transformed


def test_delegated_turn_is_not_guarded(tmp_path):
    context = _context(tmp_path)
    assert context.hooks['pre_llm_call'](
        user_message=PROGRESS, session_id='child', parent_session_id='parent') is None
    assert context.hooks['transform_llm_output'](
        response_text='内部子任务的纯文本回执', session_id='child') is None


def test_warning_is_emitted_only_once_per_turn(tmp_path):
    context = _context(tmp_path)
    context.hooks['pre_llm_call'](user_message=PROGRESS, session_id='s', turn_id='t')
    first = context.hooks['transform_llm_output'](response_text='无文档', session_id='s')
    second = context.hooks['transform_llm_output'](response_text='无文档', session_id='s')
    assert first is not None
    assert second is None


def test_audit_records_shape_without_raw_message_text(tmp_path):
    context = _context(tmp_path)
    _turn(context, PROGRESS, '无文档')

    record = _records(tmp_path)[-1]
    assert set(record) == {
        'ts', 'session_id', 'turn_id', 'platform', 'model', 'mode', 'kind',
        'reason_code', 'artifact_expected', 'artifact_present', 'guard_warned',
        'user_chars', 'response_chars',
    }
    assert record['user_chars'] == len(PROGRESS)
    assert '<business-domain>' not in json.dumps(record, ensure_ascii=False)


# --- gateway-injected notices are not user turns -------------------------------
# Production failure this pins: background-task completion notices quote the
# earlier report, so the classifier read them as "user wants a document" and the
# guard nagged on turns nobody asked anything in.

_BACKGROUND_NOTICE = (
    '[IMPORTANT: Background process proc_9c4760e7fcd5 completed normally (exit code 0).\n'
    f'Command: pytest ...\n\n[Replying to: "{PROGRESS}"]'
)


def test_background_completion_notice_is_not_a_user_turn(tmp_path):
    context = _context(tmp_path)
    advice, transformed, final = _turn(context, _BACKGROUND_NOTICE, '12 passed in 0.26s')

    assert advice is None
    assert transformed is None
    assert '交付校验未通过' not in final
    # 非用户回合不写审计记录（既不判也不告警）
    assert not (tmp_path / 'delivery_guard.jsonl').exists()


def test_internal_notification_envelope_is_not_a_user_turn(tmp_path):
    context = _context(tmp_path)
    notice = ('[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user.]\n'
              'Do not treat it as user input.\n\n' + PROGRESS)
    advice, transformed, _final = _turn(context, notice, '队列状态已刷新')

    assert advice is None
    assert transformed is None


def test_notice_turn_does_not_reuse_a_previous_turn_advice(tmp_path):
    context = _context(tmp_path)
    # Previous real turn leaves advice behind (e.g. the turn was cut short).
    context.hooks['pre_llm_call'](user_message=PROGRESS, session_id='s')
    # A notice turn must clear it instead of inheriting "document required".
    assert context.hooks['pre_llm_call'](
        user_message=_BACKGROUND_NOTICE, session_id='s') is None
    assert context.hooks['transform_llm_output'](
        response_text='没有文档的纯文本状态', session_id='s') is None
