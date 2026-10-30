import json
import math

import pytest

from agentcore.ops_alerts import AlertOutbox, DeliveryReceipt, deliver_once
from agentcore.memory.health_monitor import conditions, sample


def observe(outbox,healthy,now,**kwargs):
    return outbox.observe_health('test-profile','recall.availability',healthy=healthy,
        expected={'ready':True},observed={'ready':healthy},now=now,**kwargs)


def test_fault_and_recovery_graces_survive_observer_restart(tmp_path):
    outbox=AlertOutbox(str(tmp_path))
    assert not observe(outbox,False,0)['created']
    outbox=AlertOutbox(str(tmp_path))
    assert not observe(outbox,False,29)['created']
    fault=observe(outbox,False,30)['event']
    assert fault['kind']=='health_fault'
    assert not observe(outbox,False,80)['created']
    assert not observe(outbox,True,81)['created']
    outbox=AlertOutbox(str(tmp_path))
    recovery=observe(outbox,True,91)['event']
    assert recovery['kind']=='health_recovery' and recovery['fault_event_id']==fault['event_id']
    assert not observe(outbox,True,92)['created']
    assert observe(outbox,False,93,fault_grace=0)['event']['generation']==2
    assert len(outbox.pending())==3


def test_transient_failure_or_recovery_does_not_flap(tmp_path):
    outbox=AlertOutbox(str(tmp_path))
    observe(outbox,False,0);observe(outbox,True,29)
    assert not observe(outbox,False,30)['created']
    fault=observe(outbox,False,60)['event']
    observe(outbox,True,61);observe(outbox,False,65)
    assert not observe(outbox,True,70)['created']
    assert not observe(outbox,True,79)['created']
    assert observe(outbox,True,80)['event']['fault_event_id']==fault['event_id']


def test_queue_capacity_never_drops_pending_recovery_or_advances_state(tmp_path):
    outbox=AlertOutbox(str(tmp_path))
    observe(outbox,False,0,fault_grace=0,max_events=2,retain_delivered=0)
    observe(outbox,True,1,recovery_grace=0,max_events=2,retain_delivered=0)
    with pytest.raises(RuntimeError,match='capacity'):
        observe(outbox,False,2,fault_grace=0,max_events=2,retain_delivered=0)
    assert len(outbox.pending())==2
    with outbox.pool.db() as db:
        assert tuple(db.execute('SELECT generation,active_event_id,last_at FROM ops_health_conditions').fetchone())==(1,None,1)
    class Sender:
        def send(self,payload,*,idempotency_key):return DeliveryReceipt(True,idempotency_key,'receipt')
    assert deliver_once(outbox,Sender())['status']=='delivered'
    assert observe(outbox,False,3,fault_grace=0,max_events=2,retain_delivered=0)['created']
    assert len(outbox.pending())==2


def test_clock_regression_does_not_mature_grace(tmp_path):
    outbox=AlertOutbox(str(tmp_path));observe(outbox,False,100)
    assert not observe(outbox,False,20)['created']
    assert not observe(outbox,False,49)['created']
    assert observe(outbox,False,50)['created']


@pytest.mark.parametrize('kwargs',[{'healthy':1},{'expected':[]},{'observed':{'secret':'x'*9000}},
    {'observed':{'value':math.nan}},{'fault_grace':math.inf},{'now':-1},{'max_events':1}])
def test_invalid_health_input_is_rejected_without_event(tmp_path,kwargs):
    outbox=AlertOutbox(str(tmp_path))
    args=dict(healthy=False,expected={},observed={},now=0);args.update(kwargs)
    with pytest.raises(ValueError):outbox.observe_health('p','s',**args)
    assert outbox.pending()==[]


IDENTITY={'user_id':'owner','agent_id':'assistant','database_sha256':'digest'}


def test_health_observations_never_copy_untrusted_fields():
    health=dict(ready=True,state='ready',seq=1,identity=IDENTITY,build_sha256='build',secret='DO_NOT_COPY')
    result=conditions(health,identity=IDENTITY,build='different')
    assert result['recall.binding'][0] is False and result['recall.binding'][3] is True
    assert 'DO_NOT_COPY' not in json.dumps(result)
    health.update(ready=False,state='pending',reason_code='memory_view_embedding_pending')
    result=conditions(health,identity=IDENTITY,build='build')
    assert result['recall.availability'][2]['reason_code']=='memory_view_embedding_pending'
    health['reason_code']='DO_NOT_COPY'
    assert 'DO_NOT_COPY' not in json.dumps(conditions(health,identity=IDENTITY,build='build'))


def test_pending_fault_waits_but_blocked_is_urgent():
    for state,urgent in [('pending',False),('busy',False),('blocked',True)]:
        result=conditions(dict(ready=False,state=state,seq=1),identity=IDENTITY,build='build')
        assert result['recall.availability'][0] is False
        assert result['recall.availability'][3] is urgent
        assert result['recall.availability'][2]['state'] == state


def test_source_queue_pressure_and_missing_kernel_budget_are_detected():
    resources=dict(running=True,memory_high=3*1024**3,memory_max=0,oom_kill=0,
                   capture_installed=True,capture_events=96,capture_capacity=100)
    result=conditions(None,identity=IDENTITY,build='build',resources=resources)
    assert result['recall.resources'][0] is False and result['recall.resources'][3] is True
    assert result['recall.capture'][0] is False and result['recall.capture'][3] is True


def test_none_resource_probe_is_a_fault_not_a_silently_missing_check(tmp_path,monkeypatch):
    from agentcore.memory import health_monitor
    monkeypatch.setattr(health_monitor.recall_service,'request',lambda *a,**k:dict(
        ready=True,state='ready',seq=1,identity=IDENTITY,build_sha256='build'))
    result=sample(AlertOutbox(str(tmp_path)),tmp_path/'socket',profile='p',identity=IDENTITY,
                  build='build',resource_probe=lambda:None,now=0)
    assert not result['healthy']['recall.resources']
    assert not result['healthy']['recall.capture']
    assert not result['events_created']  # A one-off probe failure respects grace.
