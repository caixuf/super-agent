"""Named workers preserve the same trusted origin and delivery contract."""
import importlib.util
import json
from pathlib import Path

import pytest

from agentcore.pool import Pool


ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('colleague_dispatch_plugin',ROOT/'plugins/durable_dispatch/__init__.py')
plugin=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(plugin)


class Context:
    profile_name='profile-test'
    def __init__(self,settings):self.settings=settings;self.tools={};self.sections={}
    def get_config(self,key,default=None):return self.settings.get(key,default)
    def register_tool(self,**kw):self.tools[kw['name']]=kw
    def register_system_prompt_section(self,name,text,**kw):self.sections[name]=text


def build_context(tmp_path,monkeypatch):
    first=tmp_path/'analysis-sources';first.mkdir()
    second=tmp_path/'patrol-sources';second.mkdir()
    a=first/'a.txt';a.write_text('analysis evidence\n')
    b=second/'b.txt';b.write_text('patrol evidence\n')
    queues={'analysis':tmp_path/'analysis-queue','patrol':tmp_path/'patrol-queue'}
    ctx=Context(dict(enabled=True,queue_root=str(queues['analysis']),allowed_roots=[str(first)],
        deliver_completions=True,colleagues={'patrol':dict(queue_root=str(queues['patrol']),allowed_roots=[str(second)])}))
    monkeypatch.setattr(plugin,'_bound_route',lambda session:dict(route='route-'+session,channel='messaging',chat='chat-'+session))
    started=[]
    monkeypatch.setattr(plugin,'start_backfill',lambda context,service:started.append(service))
    plugin.register(ctx)
    return ctx,queues,a,b,started


def call(ctx,name,args,session='session-one'):
    return json.loads(ctx.tools['durable_'+name]['handler'](args,session_id=session))


def test_named_role_uses_own_queue_policy_origin_and_backfill(tmp_path,monkeypatch):
    ctx,queues,a,b,started=build_context(tmp_path,monkeypatch)
    assert {service.pool.root for service in started}==set(queues.values())
    args=dict(role='patrol',goal='review',request_key='one',sources=[dict(path=str(b))])
    submitted=call(ctx,'submit',args)
    assert submitted['ok'] and submitted['role']=='patrol'
    job=submitted['task_id'];origin=Pool(queues['patrol']).origin(job)
    assert origin==dict(profile='profile-test',session='session-one',route='route-session-one',
                        channel='messaging',chat='chat-session-one')
    assert call(ctx,'submit',args)['task_id']==job
    assert not call(ctx,'status',dict(task_id=job))['ok']
    assert call(ctx,'status',dict(task_id=job,role='patrol'))['ok']
    assert not call(ctx,'status',dict(task_id=job,role='patrol'),session='another')['ok']
    assert not call(ctx,'submit',dict(args,sources=[dict(path=str(a))]))['ok']
    assert not call(ctx,'submit',dict(args,role='/arbitrary/queue'))['ok']
    assert call(ctx,'cancel',dict(task_id=job,role='patrol'))['request_accepted']


def test_missing_route_is_rejected_before_any_job_is_submitted(tmp_path,monkeypatch):
    ctx,queues,a,b,_=build_context(tmp_path,monkeypatch)
    monkeypatch.setattr(plugin,'_bound_route',lambda session:{})
    # Registered services bind the route function at construction; replace that
    # trusted provider directly to emulate an unbound gateway call.
    services=[]
    monkeypatch.setattr(plugin,'start_backfill',lambda context,service:services.append(service))
    plugin.register(ctx)
    result=call(ctx,'submit',dict(role='patrol',goal='review',request_key='one',sources=[dict(path=str(b))]))
    assert not result['ok'] and 'conversation' in result['error']
    with Pool(queues['patrol']).db() as db:assert db.execute('select count(*) from jobs').fetchone()[0]==0


def test_model_cannot_supply_queue_owner_or_runner(tmp_path,monkeypatch):
    ctx,queues,a,b,_=build_context(tmp_path,monkeypatch)
    for key in ('queue_root','owner','runner'):
        result=call(ctx,'submit',dict(role='patrol',goal='review',request_key='one',sources=[dict(path=str(b))],**{key:'unsafe'}))
        assert not result['ok']


def test_duplicate_admin_queues_are_rejected(tmp_path,monkeypatch):
    ctx,queues,a,b,_=build_context(tmp_path,monkeypatch)
    ctx.settings['colleagues']['patrol']['queue_root']=str(queues['analysis'])
    with pytest.raises(ValueError,match='distinct'):plugin.register(ctx)
