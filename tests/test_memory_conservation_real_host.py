"""Real MemoryStore locking and mutation with the conservation hook, isolated home."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_real_host_stages_lossy_cron_writes_and_allows_lossless_or_reviewed_changes(tmp_path):
    host=Path(os.environ.get('HERMES_SOURCE',Path.home()/'.hermes/hermes-agent'))
    candidates=[host/'.venv/bin/python',host/'venv/bin/python']
    runtime=next((candidate for candidate in candidates if candidate.is_file()),None)
    if runtime is None:pytest.skip('Hermes host interpreter unavailable')
    engine=Path(__file__).resolve().parents[1]
    code=r'''
import importlib.util,json,sys,types
from pathlib import Path
sys.path.insert(0,sys.argv[1]);engine=Path(sys.argv[2]);home=Path(sys.argv[3])
home.mkdir(parents=True,exist_ok=True);queue=home/'conservation'
(home/'config.yaml').write_text(json.dumps({'memory':{'conservation':{'enabled':True,
 'engine_root':str(engine),'queue_root':str(queue)}}}))
from gateway.session_context import set_session_vars
from tools import memory_tool
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
 sys.modules[name]=module;spec.loader.exec_module(module);return module
load('tools.memory_conservation_guard',engine/'runtime/hermes_memory_conservation.py')
patch=load('conservation_patch',engine/'patches/patch_memory_conservation.py')
source=Path(sys.argv[1])/'tools/memory_tool_store.py'
module=types.ModuleType('conservation_store');module.__file__=str(source)
sys.modules[module.__name__]=module
exec(compile(patch.transform(source.read_text()),str(source),'exec'),module.__dict__)
store=module.MemoryStore(memory_char_limit=20000,user_char_limit=5000)
memory=home/'memories/MEMORY.md';memory.parent.mkdir(parents=True,exist_ok=True)
memory.write_text('【预算】最大并发七个\n§\n【人物】阿蓝')
store.load_from_disk();original=memory.read_text()
set_session_vars(cron_session='1')
blocked=store.remove('memory','【人物】阿蓝')
assert not blocked['success'] and blocked['conservation_pending']
assert memory.read_text()==original and '【人物】阿蓝' in store.memory_entries
assert store.add('memory','【回执】保留完整回执')['success']
set_session_vars(cron_session='')
assert store.remove('memory','【人物】阿蓝')['success']
# A real semantic review can authorize exactly its own before/after revision.
set_session_vars(cron_session='1')
before=memory.read_text();proposed=store.patch('memory','最大并发七个','并发最多七个')
assert proposed['conservation_pending']
directory=queue/'pending'/proposed['proposal_id']
after=(directory/'candidate.md').read_text()
c=load('conservation_engine',engine/'agentcore/memory/conservation.py')
decision=[dict(id=a['id'],decision='equivalent',source='candidate',quote='并发最多七个',reason='same concurrency bound')
          for a in c.clauses(before) if a['quote']=='最大并发七个']
assert len(decision)==1
review=dict(before_sha256=c.sha(before),after_sha256=c.sha(after),references={},reviewer='administrator-review',decisions=decision)
(queue/'reviewed').mkdir();(queue/'reviewed'/(proposed['proposal_id']+'.json')).write_text(json.dumps(review))
assert store.patch('memory','最大并发七个','并发最多七个')['success']
assert memory.read_text()==after
# A stale approval cannot authorize another revision; gate failures must not
# mutate either the file or the in-process entries.
assert store.add('memory','【新增】当前版本事实')['success']
newer=memory.read_text()
stale=store.patch('memory','并发最多七个','七个并发')
assert stale['conservation_pending']
(queue/'reviewed'/(stale['proposal_id']+'.json')).write_text(json.dumps(review))
assert store.patch('memory','并发最多七个','七个并发')['conservation_pending']
assert memory.read_text()==newer
config=json.loads((home/'config.yaml').read_text())
config['memory']['conservation']['references']={'missing':dict(path=str(home/'missing'),pointer='missing')}
(home/'config.yaml').write_text(json.dumps(config))
failure=store.remove('memory','【新增】当前版本事实')
assert not failure['success'] and memory.read_text()==newer
assert '【新增】当前版本事实' in store.memory_entries
# Profiles without opt-in retain their existing behavior.
(home/'config.yaml').write_text('{}')
assert store.remove('memory','【新增】当前版本事实')['success']
# The actual runtime bridge distinguishes capacity refusal from a saved
# candidate and never falls back to overwriting the memory source.
(home/'config.yaml').write_text(json.dumps({'memory':{'conservation':{'enabled':True,
 'engine_root':str(engine),'queue_root':str(queue)}}}))
index=0
while len(c.inventory(queue/'pending'))<128:
 candidate='隔离容量样本'+str(index);index+=1
 c.stage(queue,memory,newer,candidate,c.evaluate(newer,candidate))
current=memory.read_text()
capacity=store.remove('memory','【回执】保留完整回执')
assert not capacity['success'] and capacity['conservation_capacity_exceeded'] and capacity['staged'] is False
assert memory.read_text()==current and '【回执】保留完整回执' in store.memory_entries
assert patch.transform(patch.transform(source.read_text()))==patch.transform(source.read_text())
proof=dict(passed=True,lossy_cron_staged=True,source_and_live_entries_preserved=True,
           lossless_cron_add_applied=True,foreground_unchanged=True,exact_review_applied=True,
           stale_review_refused=True,missing_reference_refused=True,unconfigured_profile_unchanged=True,
           capacity_refusal_explicit_and_source_unchanged=True)
(home/'proof.json').write_text(json.dumps(proof));print(json.dumps(proof))
'''
    result=subprocess.run([str(runtime),'-c',code,str(host),str(engine),str(tmp_path/'home')],
                          env=dict(os.environ,HERMES_HOME=str(tmp_path/'home')),capture_output=True,text=True,timeout=120)
    assert result.returncode==0,result.stderr[-4000:]+result.stdout[-1000:]
    assert json.loads(result.stdout.strip().splitlines()[-1])['passed']
