import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('team_plugin', ROOT / 'plugins/colleague_team/__init__.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class Context:
    def __init__(self, root, profile='leader', enabled=True):
        self.profile_name = profile
        self.settings = {'enabled': enabled, 'registry_path': str(root / 'team.sqlite'),
                         'team_id': 'team', 'primary_id': 'leader', 'engine_root': str(ROOT)}
        self.tools = {}
        self.sections = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_system_prompt_section(self, name, text, **kwargs):
        self.sections[name] = text


def call(ctx, name, args, session='session-1'):
    return json.loads(ctx.tools['colleague_' + name]['handler'](args, session_id=session))


def test_main_hires_manager_who_hires_and_delegates_but_cannot_retire_main(tmp_path):
    primary = Context(tmp_path)
    plugin.register(primary)
    assert call(primary, 'define_role', {'role_id': 'people', 'label': 'People',
        'duty': 'Build a team', 'boundaries': ['Follow the host policy'], 'request_id': 'role-1'})['ok']
    assert call(primary, 'hire', {'member_id': 'recruiter', 'role_id': 'people',
        'display_name': 'Recruiter', 'request_id': 'hire-1'})['provisioning_required']
    assert call(primary, 'grant', {'member_id': 'recruiter', 'enabled': True, 'request_id': 'grant-1'})['ok']
    recruiter = Context(tmp_path, 'recruiter')
    plugin.register(recruiter)
    assert call(recruiter, 'hire', {'member_id': 'recruiter_two', 'role_id': 'people',
        'display_name': 'Recruiter two', 'request_id': 'hire-2'})['ok']
    assert call(recruiter, 'grant', {'member_id': 'recruiter_two', 'enabled': True, 'request_id': 'grant-2'})['ok']
    assert call(recruiter, 'retire', {'member_id': 'leader', 'request_id': 'retire-primary'}) == {
        'ok': False, 'error': 'primary_protected'}
    assert call(primary, 'retire', {'member_id': 'leader', 'request_id': 'self-retire'}) == {
        'ok': False, 'error': 'primary_protected'}
    roster = call(primary, 'list', {})
    assert roster['ok'] and {m['member_id'] for m in roster['members']} == {'leader', 'recruiter', 'recruiter_two'}
    assert next(m for m in roster['members'] if m['member_id'] == 'recruiter')['runtime_state'] == 'unknown'


def test_model_cannot_choose_actor_and_unprivileged_profile_cannot_manage(tmp_path):
    primary = Context(tmp_path)
    plugin.register(primary)
    call(primary, 'define_role', {'role_id': 'review', 'label': 'Review', 'duty': 'Review work',
                                 'boundaries': ['Read only'], 'request_id': 'role-1'})
    call(primary, 'hire', {'member_id': 'reviewer', 'role_id': 'review',
                           'display_name': 'Reviewer', 'request_id': 'hire-1'})
    member = Context(tmp_path, 'reviewer')
    plugin.register(member)
    assert call(member, 'list', {})['ok']
    page = call(member, 'list', {'limit': 1})
    assert page['ok'] and len(page['members']) == 1 and page['member_next_after']
    args = {'member_id': 'other', 'role_id': 'review', 'display_name': 'Other', 'request_id': 'hire-2'}
    assert not call(member, 'hire', args)['ok']
    assert call(member, 'hire', {**args, 'actor': 'leader'}) == {'ok': False, 'error': 'invalid_arguments'}
    assert call(primary, 'list', {}, session=None) == {'ok': False, 'error': 'team_session_required'}


def test_retire_requests_drain_and_does_not_claim_worker_stopped(tmp_path):
    primary = Context(tmp_path)
    plugin.register(primary)
    call(primary, 'define_role', {'role_id': 'review', 'label': 'Review', 'duty': 'Review work',
                                 'boundaries': ['Read only'], 'request_id': 'role-1'})
    call(primary, 'hire', {'member_id': 'reviewer', 'role_id': 'review',
                           'display_name': 'Reviewer', 'request_id': 'hire-1'})
    result = call(primary, 'retire', {'member_id': 'reviewer', 'request_id': 'retire-1'})
    assert result['ok'] and result['status'] == 'draining'
    assert 'host must drain' in result['note']
    assert call(primary, 'retire', {'member_id': 'reviewer', 'request_id': 'retire-1'})['replayed']


def test_disabled_plugin_has_no_tools(tmp_path):
    ctx = Context(tmp_path, enabled=False)
    plugin.register(ctx)
    assert ctx.tools == {}


def test_team_tools_and_supervisor_hire_delegate_drain_and_preserve_history(tmp_path):
    from agentcore.colleague_provisioner import Provisioner
    from agentcore.colleague_registry import Actor, Registry, Supervisor

    class Backend:
        def __init__(self):
            self.members = {}

        def ensure(self, spec):
            self.members[spec.member_id] = 'ready'

        def inspect(self, spec):
            return {'state': self.members.get(spec.member_id, 'absent'),
                    'isolated': True, 'private_memory': True, 'no_channels': True}

        def idle(self, spec):
            return True

        def park(self, spec):
            self.members[spec.member_id] = 'parked'

    primary = Context(tmp_path)
    plugin.register(primary)
    role = {'role_id': 'people', 'label': 'People', 'duty': 'Build the team',
            'boundaries': ['Follow host policy'], 'request_id': 'role-1'}
    assert call(primary, 'define_role', role)['ok']
    assert call(primary, 'hire', {'member_id': 'recruiter', 'role_id': 'people',
        'display_name': 'Recruiter', 'request_id': 'hire-1'})['ok']
    registry = Registry(tmp_path / 'team.sqlite', profile_id='team', primary_id='leader')
    state = tmp_path / 'lifecycle'
    state.mkdir(mode=0o700)
    backend = Backend()
    supervisor = Provisioner(registry, supervisor=Supervisor('team'), backend=backend, state_dir=state)
    assert supervisor.provision('recruiter', 1)['ready']
    assert call(primary, 'grant', {'member_id': 'recruiter', 'enabled': True, 'request_id': 'grant-1'})['ok']
    recruiter = Context(tmp_path, 'recruiter')
    plugin.register(recruiter)
    assert call(recruiter, 'define_role', {'role_id': 'builder', 'label': 'Builder',
        'duty': 'Build requested changes', 'boundaries': ['Follow host policy'], 'request_id': 'role-2'})['ok']
    assert call(recruiter, 'hire', {'member_id': 'builder_one', 'role_id': 'builder',
        'display_name': 'Builder one', 'request_id': 'hire-2'})['ok']
    assert supervisor.provision('builder_one', 1)['ready']
    assert call(recruiter, 'retire', {'member_id': 'builder_one', 'request_id': 'retire-1'})['status'] == 'draining'
    stopped = supervisor.reconcile_deactivation('builder_one', 1)
    assert stopped['membership_status'] == 'inactive' and stopped['phase'] == 'parked'
    assert backend.members['builder_one'] == 'parked' and backend.members['recruiter'] == 'ready'
    events = registry.history(actor=Actor('team', 'leader'))
    assert any(item['action'] == 'member_hired' and item['target_id'] == 'builder_one' for item in events)
    assert call(primary, 'retire', {'member_id': 'leader', 'request_id': 'retire-primary'})['error'] == 'primary_protected'
