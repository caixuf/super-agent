"""Administrative provenance repair cannot redirect or re-execute existing work."""
import hashlib
import json

import pytest

from agentcore.pool import Pool


ORIGIN=dict(profile='profile-test',session='session-one',route='route-one')


def complete(pool,job):
    # Isolated fixture: origin recovery must not itself manufacture completion.
    with pool.db() as db:
        db.execute("update jobs set status='completed',result=? where id=?",(json.dumps({'ok':True}),job))
        db.execute('insert into outbox(job,receipt) values(?,?)',(job,json.dumps({'job':job})))


def args(pool,job):
    with pool.db() as db:payload=db.execute('select payload from jobs where id=?',(job,)).fetchone()[0]
    return dict(origin=ORIGIN,expected_payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                proof_id='request-and-tool-receipt',proof_sha256='a'*64)


def test_recovery_is_idempotent_audited_and_preserves_result_and_ack(tmp_path):
    pool=Pool(tmp_path);job=pool.submit('one',{'goal':'review'});complete(pool,job)
    before=pool.status(job)
    assert not pool.attach_origin(job,**args(pool,job))['already_attached']
    pool=Pool(tmp_path)
    assert pool.attach_origin(job,**args(pool,job))['already_attached']
    assert pool.status(job)==before
    assert pool.origin(job)==ORIGIN
    with pool.db() as db:
        assert db.execute('select count(*) from origin_repairs where job=?',(job,)).fetchone()[0]==1
        assert db.execute('select acknowledged from outbox where job=?',(job,)).fetchone()[0]==0
    with pytest.raises(ValueError,match='conflict'):
        pool.attach_origin(job,**dict(args(pool,job),origin=dict(ORIGIN,route='another')))


@pytest.mark.parametrize('case',['queued','owned','changed_payload','leased','acked'])
def test_unproven_or_started_delivery_cannot_be_repaired(tmp_path,case):
    pool=Pool(tmp_path);job=pool.submit('one',{'goal':'review'},origin=ORIGIN if case=='owned' else None)
    if case!='queued':complete(pool,job)
    values=args(pool,job)
    if case=='changed_payload':values['expected_payload_sha256']='b'*64
    if case in ('leased','acked'):
        with pool.db() as db:
            if case=='leased':db.execute("update outbox set lease_token='token' where job=?",(job,))
            else:db.execute('update outbox set acknowledged=1 where job=?',(job,))
    with pytest.raises(ValueError):pool.attach_origin(job,**values)
    with pool.db() as db:assert db.execute('select count(*) from origin_repairs').fetchone()[0]==0
