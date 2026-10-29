from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from agentcore.memory import conservation as c
from agentcore.memory import proposal_archive as a


def stage(root,source,after,**kwargs):
    before=source.read_text()
    return c.stage(root,source,before,after,c.evaluate(before,after),**kwargs)


def test_count_and_bytes_budgets_refuse_without_deleting_source_or_older_candidates(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('保留原始事实')
    root=tmp_path/'queue';first=stage(root,source,'候选一',max_proposals=1)
    assert stage(root,source,'候选一',max_proposals=1)==first
    with pytest.raises(ValueError,match='capacity'):stage(root,source,'候选二',max_proposals=1)
    with pytest.raises(ValueError,match='capacity'):stage(root,source,'候选三',max_bytes=1)
    assert source.read_text()=='保留原始事实'
    assert a.status(root)['pending']['proposals']==1


def test_concurrent_writers_cannot_overbook_the_last_slot(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('不能丢失')
    root=tmp_path/'queue'
    def attempt(value):
        try:return bool(stage(root,source,value,max_proposals=1))
        except ValueError as error:
            assert str(error)=='proposal_capacity_exceeded';return False
    with ThreadPoolExecutor(max_workers=2) as workers:result=list(workers.map(attempt,['候选甲','候选乙']))
    assert sorted(result)==[False,True]
    assert a.status(root)['pending']['proposals']==1 and source.read_text()=='不能丢失'


def test_archive_restore_roundtrip_frees_online_slot_without_touching_source(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('仍有效的事实')
    root=tmp_path/'queue';item=stage(root,source,'待审的压缩')
    original={p.name:p.read_bytes() for p in (root/'pending'/item['id']).iterdir()}
    archived=a.archive(root,item['id'])
    assert a.archive(root,item['id'])==archived
    assert a.status(root)['pending']['proposals']==0
    assert a.status(root)['archive']['proposals']==1
    restored=a.restore(root,item['id'])
    assert restored['manifest_sha256']==archived['manifest_sha256']
    assert a.restore(root,item['id'])==restored
    assert {p.name:p.read_bytes() for p in (root/'pending'/item['id']).iterdir()}==original
    assert source.read_text()=='仍有效的事实'


def test_archive_full_or_corrupt_never_retires_the_pending_original(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('原文')
    root=tmp_path/'queue';item=stage(root,source,'待审')
    with pytest.raises(ValueError,match='capacity'):a.archive(root,item['id'],max_bytes=1)
    assert (root/'pending'/item['id']/'before.md').exists()
    a.archive(root,item['id']);a.restore(root,item['id'])
    archived=root/'archive'/item['id']/'candidate.md';archived.write_text('篡改')
    with pytest.raises(ValueError,match='checksum'):a.archive(root,item['id'])
    assert (root/'pending'/item['id']/'before.md').read_text()=='原文'
    with pytest.raises(ValueError,match='checksum'):a.restore(root,item['id'])


def test_restore_budget_and_existing_conflict_preserve_both_versions(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('旧版')
    root=tmp_path/'queue';item=stage(root,source,'压缩候选');a.archive(root,item['id'])
    with pytest.raises(ValueError,match='capacity'):a.restore(root,item['id'],max_bytes=1)
    other=stage(root,source,'别的候选',max_proposals=1)
    with pytest.raises(ValueError,match='capacity'):a.restore(root,item['id'],max_proposals=1)
    assert (root/'pending'/other['id']).exists()
    a.restore(root,item['id']);(root/'pending'/item['id']/'candidate.md').write_text('别的版本')
    with pytest.raises(ValueError,match='conflict'):a.restore(root,item['id'])
    assert (root/'pending'/item['id']/'candidate.md').read_text()=='别的版本'
    assert source.read_text()=='旧版'


def test_symlinks_and_path_traversal_are_refused(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('原文')
    root=tmp_path/'queue';item=stage(root,source,'候选')
    with pytest.raises(ValueError,match='id'):a.archive(root,'../../escape')
    pending=root/'pending'/item['id'];(pending/'candidate.md').unlink();(pending/'candidate.md').symlink_to(source)
    with pytest.raises(ValueError,match='nonregular'):a.archive(root,item['id'])
    with pytest.raises(ValueError,match='nonregular'):stage(root,source,'另一个候选')
    assert source.read_text()=='原文'


def test_archive_rejects_incomplete_or_mismatched_provenance(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('原文')
    root=tmp_path/'queue';item=stage(root,source,'候选')
    report=next((root/'pending'/item['id']).glob('report-*.json'))
    record=json.loads(report.read_text());record['before_sha256']='0'*64
    text=json.dumps(record);report.unlink();(report.parent/('report-'+c.sha(text)+'.json')).write_text(text)
    with pytest.raises(ValueError,match='revision'):a.archive(root,item['id'])
    assert not (root/'archive'/item['id']).exists()


def test_interrupted_copy_and_partial_retirement_resume_from_verified_archive(tmp_path):
    source=tmp_path/'MEMORY.md';source.write_text('必须可恢复')
    root=tmp_path/'queue';item=stage(root,source,'待核对')
    pending=root/'pending'/item['id'];destination=root/'archive'/item['id']
    destination.mkdir(parents=True)
    (destination/'before.md').write_bytes((pending/'before.md').read_bytes())
    # Simulate stopping while copying, before the commit manifest was written.
    archived=a.archive(root,item['id']);a.restore(root,item['id'])
    # Simulate stopping after one online payload was removed.
    (pending/'before.md').unlink()
    assert a.archive(root,item['id'])==archived
    assert not pending.exists()
    a.restore(root,item['id'])
    assert (pending/'before.md').read_text()=='必须可恢复'
    assert source.read_text()=='必须可恢复'
