import pytest

from agentcore.colleague_conversation import ConversationRouter
from agentcore.colleague_registry import Actor, Registry, Supervisor


@pytest.fixture
def team(tmp_path):
    registry = Registry(tmp_path / 'roster.sqlite', profile_id='team', primary_id='leader')
    actor, supervisor = Actor('team', 'leader'), Supervisor('team')
    registry.define_role(actor=actor, role_id='developer', label='Developer', duty='Review',
                         boundaries=['Read only'], request_id='role')
    for member, name in [('dev_a', '小程'), ('dev_b', '小程'), ('reviewer', '小审')]:
        registry.hire(actor=actor, member_id=member, role_id='developer', display_name=name,
                      request_id='hire-' + member)
        registry.observe_runtime(supervisor=supervisor, member_id=member, generation=1, state='running')
    accepted = set()
    allowed = {'dev_a', 'dev_b', 'reviewer'}

    def authorize(owner, member):
        return member['member_id'] in allowed

    def verify(owner, sequence, member, generation, work):
        return (owner['session'], sequence, member, generation, work) in accepted

    def router():
        return ConversationRouter(tmp_path / 'dialogue.sqlite', registry, supervisor,
                                  authorize=authorize, verify_assignment=verify)

    return registry, actor, supervisor, router, accepted, allowed


def test_name_disambiguation_and_access_filter(team):
    _, _, _, make, _, allowed = team
    router = make()
    owner = {'profile': 'team', 'session': 'one'}
    result = router.select(owner, 1, target={'name': '小程'})
    assert result['state'] == 'ambiguous'
    assert [m['member_id'] for m in result['candidates']] == ['dev_a', 'dev_b']
    allowed.remove('dev_b')
    assert router.select(owner, 2, target={'name': '小程'})['member_id'] == 'dev_a'
    router.abandon(owner, 2)
    assert router.select(owner, 3, target={'id': 'dev_b'}) == {'state': 'target_unavailable'}


def test_followup_uses_verified_assignment_and_survives_restart(team):
    _, _, _, make, accepted, _ = team
    owner = {'profile': 'team', 'session': 'one'}
    other = {'profile': 'team', 'session': 'two'}
    router = make()
    selected = router.select(owner, 1, target={'id': 'dev_a'})
    assert selected['state'] == 'selected'
    with pytest.raises(PermissionError, match='assignment_unverified'):
        router.remember(owner, 1, member_id='dev_a', generation=1, work_id='work-1')
    accepted.add(('one', 1, 'dev_a', 1, 'work-1'))
    assert router.remember(owner, 1, member_id='dev_a', generation=1, work_id='work-1')['state'] == 'remembered'
    resumed = make().select(owner, 2)
    assert resumed['member_id'] == 'dev_a'
    assert resumed['conversation_key'] == selected['conversation_key']
    assert make().select(other, 2) == {'state': 'target_required'}
    with pytest.raises(ValueError, match='sequence_conflict'):
        make().select(owner, 1)
    assert make().select(other, 3, target={'id': 'dev_a'})['conversation_key'] != selected['conversation_key']


def test_out_of_order_completion_cannot_change_last_target(team):
    _, _, _, make, accepted, _ = team
    owner = {'profile': 'team', 'session': 'one'}
    router = make()
    accepted.update({('one', 1, 'dev_a', 1, 'a'), ('one', 2, 'reviewer', 1, 'b'),
                     ('one', 2, 'dev_b', 1, 'conflict')})
    assert router.select(owner, 1, target={'id': 'dev_a'})['state'] == 'selected'
    assert router.select(owner, 2, target={'id': 'reviewer'})['state'] == 'prior_turn_pending'
    router.remember(owner, 1, member_id='dev_a', generation=1, work_id='a')
    assert router.select(owner, 2, target={'id': 'reviewer'})['state'] == 'selected'
    router.remember(owner, 2, member_id='reviewer', generation=1, work_id='b')
    assert router.remember(owner, 1, member_id='dev_a', generation=1, work_id='a')['state'] == 'superseded'
    assert router.remember(owner, 2, member_id='reviewer', generation=1, work_id='b')['replayed']
    with pytest.raises(ValueError, match='assignment_selection_mismatch'):
        router.remember(owner, 2, member_id='dev_b', generation=1, work_id='conflict')
    assert router.select(owner, 3)['member_id'] == 'reviewer'


def test_retirement_and_lost_authorization_fail_closed(team):
    registry, actor, _, make, accepted, allowed = team
    owner = {'profile': 'team', 'session': 'one'}
    router = make()
    accepted.add(('one', 1, 'dev_a', 1, 'work-1'))
    router.select(owner, 1, target={'id': 'dev_a'})
    router.remember(owner, 1, member_id='dev_a', generation=1, work_id='work-1')
    allowed.remove('dev_a')
    assert router.select(owner, 2) == {'state': 'target_unavailable'}
    allowed.add('dev_a')
    registry.request_deactivation(actor=actor, member_id='dev_a', request_id='retire')
    assert router.select(owner, 3) == {'state': 'target_unavailable'}


def test_owner_and_target_validation(team):
    _, _, _, make, _, _ = team
    router = make()
    with pytest.raises(PermissionError):
        router.select({'profile': 'other', 'session': 'one'}, 1, target={'id': 'dev_a'})
    with pytest.raises(ValueError):
        router.select({'profile': 'team', 'session': 'one'}, 1, target={'id': 'dev_a', 'name': '小程'})
    with pytest.raises(ValueError):
        router.select({'profile': 'team', 'session': 'one'}, True, target={'id': 'dev_a'})


def test_separate_owner_profile_and_database_scope(team, tmp_path):
    registry, _, supervisor, _, _, _ = team
    path = tmp_path / 'another-scope.sqlite'
    allow = lambda owner, member: True
    verify = lambda owner, sequence, member, generation, work: True
    router = ConversationRouter(path, registry, supervisor, authorize=allow,
                                verify_assignment=verify, owner_profile='chat-profile')
    assert router.select({'profile': 'chat-profile', 'session': 'one'}, 1,
                         target={'id': 'dev_a'})['member_id'] == 'dev_a'
    with pytest.raises(PermissionError):
        router.select({'profile': 'team', 'session': 'one'}, 1, target={'id': 'dev_a'})
    with pytest.raises(ValueError, match='conversation_scope_mismatch'):
        ConversationRouter(path, registry, supervisor, authorize=allow, verify_assignment=verify)
