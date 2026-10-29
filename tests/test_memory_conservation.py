import json

import pytest

from agentcore.memory import conservation as c


def review(before,after,decision,*,references=None):
    return dict(before_sha256=c.sha(before),after_sha256=c.sha(after),
                references={k:c.reference_fingerprint(v) for k,v in (references or {}).items()},
                reviewer='administrator-review',decisions=[dict(decision,id=c.clauses(before)[0]['id'])])


def test_literal_additions_and_dedup_keep_every_clause_including_short_names():
    before='【人物】阿蓝。\n§\n【预算】上限 >= 3.0。'
    assert c.evaluate(before,before+'\n新增一条事实。')['passed']
    assert not c.evaluate(before,'【预算】上限 >= 3.0。')['passed']
    assert c.evaluate('阿蓝。阿蓝。','阿蓝。')['passed']


def test_normalization_keeps_comparison_direction_and_ascii_word_boundaries():
    assert c.normalize('上限 ≥ 3.0')==c.normalize('上限>=3.0')
    assert not c.evaluate('上限 >= 3.0','上限 <= 3.0')['passed']
    assert not c.evaluate('use alpha beta','use alphabeta')['passed']


def test_skill_reference_requires_an_actual_pointer_in_candidate():
    refs={'skill':dict(text='完整资料：上限为七个。',pointer='skill/operations')}
    assert c.evaluate('上限为七个','细节见 skill/operations',references=refs)['passed']
    assert not c.evaluate('上限为七个','细节略',references=refs)['passed']


def test_review_is_pinned_to_both_source_versions_and_reference_bytes():
    before='并发上限为七个';after='最多同时执行七个'
    manifest=review(before,after,dict(decision='equivalent',source='candidate',quote=after,reason='same upper bound'))
    assert c.evaluate(before,after,review=manifest)['passed']
    assert not c.evaluate(before,after+'新结论',review=manifest)['passed']
    assert not c.evaluate(before+'另一条',after,review=manifest)['passed']
    assert not c.evaluate(before,after,review=dict(approved=True,decisions=manifest['decisions']))['passed']


@pytest.mark.parametrize('after',['门限 <= 3.0','门限 >= 5.0'])
def test_semantic_review_cannot_silently_change_numeric_or_operator_invariants(after):
    before='门限 >= 3.0'
    manifest=review(before,after,dict(decision='equivalent',source='candidate',quote=after,reason='claimed equivalent'))
    assert not c.evaluate(before,after,review=manifest)['passed']


def test_retirement_requires_a_current_authority_reference_not_the_candidate_itself():
    before='旧门限 3.0';after='新门限 5.0。依据 current-config'
    refs={'config':dict(text='当前门限明确为 5.0',pointer='current-config',authority=True)}
    record=dict(decision='retired',source='config',quote=refs['config']['text'],reason='verified changed configuration')
    manifest=review(before,after,record,references=refs)
    assert c.evaluate(before,after,references=refs,review=manifest)['passed']
    changed={'config':dict(refs['config'],text='当前门限为 8.0')}
    assert not c.evaluate(before,after,references=changed,review=manifest)['passed']
    untrusted={'config':dict(refs['config'],authority=False)}
    assert not c.evaluate(before,after,references=untrusted,review=manifest)['passed']
    false_review=review(before,after,dict(record,source='candidate',quote=after))
    assert not c.evaluate(before,after,review=false_review)['passed']


def test_evidence_must_be_a_real_exact_quote():
    before='并发上限为七个';after='最多同时执行七个'
    manifest=review(before,after,dict(decision='equivalent',source='candidate',quote='invented evidence',reason='same'))
    assert not c.evaluate(before,after,review=manifest)['passed']


def test_review_can_drop_list_ordinals_but_not_thresholds_or_measurements():
    before='2) 门限 >= 3.0';after='当前门限 >= 3.0'
    manifest=review(before,after,dict(decision='equivalent',source='candidate',quote=after,reason='only list label removed'))
    assert c.evaluate(before,after,review=manifest)['passed']
    assert c.protected('3.0 秒')[0]=={'3.0'}
    assert c.protected('规则：1) 门限 >= 3.0')[0]=={'1','3.0'}
    assert c.protected('调度中枢 127.0.0.1:9900、唯一入口')[0]=={'127.0.0.1','9900'}
    changed='当前门限 >= 5.0'
    manifest=review(before,changed,dict(decision='equivalent',source='candidate',quote=changed,reason='claimed same'))
    assert not c.evaluate(before,changed,review=manifest)['passed']


def test_staging_is_durable_idempotent_and_preserves_source(tmp_path):
    source=tmp_path/'MEMORY.md';before='保留此事实';after='删掉后另写';source.write_text(before)
    report=c.evaluate(before,after)
    first=c.stage(tmp_path/'queue',source,before,after,report)
    second=c.stage(tmp_path/'queue',source,before,after,report)
    assert first==second and source.read_text()==before
    assert (tmp_path/'queue/pending'/first['id']/'before.md').read_text()==before
    saved=json.loads(open(first['report_path']).read())
    assert saved['unresolved'] and not saved['passed']
