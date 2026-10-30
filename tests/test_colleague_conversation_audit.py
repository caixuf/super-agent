"""Adversarial tests for colleague conversation target selection."""
from agentcore.colleague_conversation import ConversationRouter
from agentcore.colleague_registry import Actor, Registry, Supervisor


def _team(tmp_path):
    registry = Registry(tmp_path / 'roster.sqlite', profile_id='team', primary_id='leader')
    actor, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='developer', label='Dev', duty='Read',
                         boundaries=['Read only'], request_id='role')
    for member in ('a', 'b'):
        registry.hire(actor=actor, member_id=member, role_id='developer', display_name='小程',
                      request_id='hire-' + member)
        registry.observe_runtime(supervisor=supervisor, member_id=member, generation=1, state='running')
    router = ConversationRouter(tmp_path / 'dialogue.sqlite', registry, supervisor,
                                authorize=lambda owner, member: True,
                                verify_assignment=lambda owner, sequence, member, generation, work: True)
    return registry, actor, router


def test_same_name_retired_member_does_not_block_ready_member(tmp_path):
    registry, actor, router = _team(tmp_path)
    registry.request_deactivation(actor=actor, member_id='b', request_id='retire-b')
    result = router.select({'profile': 'team', 'session': 'one'}, 1, target={'name': '小程'})
    assert result['state'] == 'selected'
    assert result['member_id'] == 'a'


def test_implicit_followup_must_not_bypass_inflight_previous_selection(tmp_path):
    _, _, router = _team(tmp_path)
    owner = {'profile': 'team', 'session': 'one'}
    assert router.select(owner, 1, target={'id': 'a'})['member_id'] == 'a'
    router.remember(owner, 1, member_id='a', generation=1, work_id='work-a')
    assert router.select(owner, 2, target={'id': 'b'})['member_id'] == 'b'
    # Work for sequence 2 has not been admitted or remembered yet. The next
    # implicit follow-up must not silently pick the previous member a.
    assert router.select(owner, 3)['state'] == 'prior_turn_pending'
    router.remember(owner, 2, member_id='b', generation=1, work_id='work-b')
    assert router.select(owner, 3)['member_id'] == 'b'


def test_reselect_cannot_abandon_a_possibly_admitted_assignment(tmp_path):
    registry, actor, router = _team(tmp_path)
    owner = {'profile': 'team', 'session': 'one'}
    assert router.select(owner, 1, target={'id': 'a'})['member_id'] == 'a'
    # A concurrent retry observes that the selected member is being retired.
    # The first submit may already have succeeded, so retry cannot abandon it.
    registry.request_deactivation(actor=actor, member_id='a', request_id='retire-a')
    assert router.select(owner, 1, target={'id': 'a'})['state'] != 'selected'
    assert router.remember(owner, 1, member_id='a', generation=1, work_id='work-a')['state'] == 'remembered'
