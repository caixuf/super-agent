"""Real host registration, route resolver and outbox; fake platform I/O only."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_real_host_routes_directly_without_a_model_turn(tmp_path):
    host = Path(os.environ.get('HERMES_SOURCE', Path.home()/'.hermes/hermes-agent'))
    runtime = host/'.venv/bin/python'
    if not runtime.is_file():
        pytest.skip('Hermes host interpreter unavailable')
    engine = Path(__file__).resolve().parents[1]
    code = r'''
import asyncio,importlib.util,json,os,sys,threading,time
from pathlib import Path
from types import SimpleNamespace as NS
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli import config as config_mod
from hermes_cli import plugin_messages as host_messages
from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
from hermes_cli.plugins_state import save_plugin_setting
from agentcore.pool import Pool

home=Path(sys.argv[3]);home.mkdir(parents=True,exist_ok=True)
root=home/'queue'; engine=Path(sys.argv[2]); route='stored-route'
config_mod.save_config({'plugins':{'entries':{'durable-dispatch':{}}}},merge_existing=True)
settings=dict(enabled=True,engine_root=str(engine),queue_root=str(root),allowed_roots=[str(home)],
              deliver_completions=True,direct_delivery_enabled=True,delivery_interval=1,
              delivery_transport_file=str(engine/'runtime/hermes_completion_transport.py'))
for key,value in settings.items():save_plugin_setting('durable-dispatch',(key,),value)
pool=Pool(root)
# The host derives the profile name from HERMES_HOME: unset/~/.hermes is 'default',
# ~/.hermes/profiles/<name> is <name>, and any other path (a temp home like this one)
# is 'custom'. A hard-coded name stops matching the moment the home moves, so derive it.
from hermes_cli.profiles import get_active_profile_name
profile=get_active_profile_name()
origin=dict(profile=profile,session='source-session',route=route,channel='feishu',chat='chat-1',thread='thread-1')
job=pool.submit('real-host-direct',{'goal':'read-only review'},origin=origin)
claimed,fd=pool.claim()
directory=root/'attempts'/claimed['attempt'];directory.mkdir(parents=True,exist_ok=True)
report=directory/'report.md';report.write_text('source-grounded analysis')
pool.finish(job,claimed['attempt'],True,{'ok':True,'result':{'summary':'actual report summary','artifacts':[str(report)]}})
os.close(fd)
calls=[];checks=[]
class Adapter:
    DURABLE_DELIVERY_UUID=True
    def max_message_length_for_chat(self,chat):return 4096
    def message_len_fn_for_chat(self,chat):return len
    async def send(self,chat,text,**kw):
        assert 'actual report summary' in text
        calls.append(('text',chat,kw));return NS(success=True,message_id='platform-summary')
    async def send_document(self,chat,path,**kw):
        assert Path(path).read_text()=='source-grounded analysis'
        calls.append(('file',chat,kw));return NS(success=True,message_id='platform-file')
adapter=Adapter()
source=SessionSource(platform=Platform.FEISHU,chat_id='chat-1',thread_id='thread-1')
def owned(key,plugin_home,plugin_id):
    checks.append((key,str(plugin_home),plugin_id))
    return key==route and Path(plugin_home)==home and plugin_id=='durable-dispatch'
runner=NS(session_store=NS(lookup_by_session_key=lambda key:NS(origin={}) if key==route else None),
          _restored_source=lambda entry:source,_plugin_messaging_session_owned=owned,
          _delivery_adapter_for=lambda source:adapter,_thread_metadata_for_source=lambda source:{'thread_id':'thread-1'})
loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
host_messages._gateway_loop=lambda:(runner,loop)
spec=importlib.util.spec_from_file_location('direct_plugin',engine/'plugins/durable_dispatch/__init__.py')
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
manifest=PluginManifest(name='durable-dispatch',version='0.2.0',description='test',author='test',path=engine/'plugins/durable_dispatch')
manager=PluginManager(scope_key=str(home))
def forbidden(**kw):raise AssertionError('completion started a model turn')
manager.set_gateway_message_injector(object(),forbidden)
ctx=PluginContext(manifest,manager)
stops=[];ctx.on_unload=stops.append
plugin.register(ctx)
deadline=time.monotonic()+10
while time.monotonic()<deadline and not pool.delivery_record(job)['acknowledged']:time.sleep(.05)
record=pool.delivery_record(job)
for callback in stops:callback()
loop.call_soon_threadsafe(loop.stop);thread.join(3);loop.close()
assert record['acknowledged']==1,record
assert [c[0] for c in calls]==['text','file'],calls
assert all(c[1]=='chat-1' and c[2]['metadata']['thread_id']=='thread-1' for c in calls)
assert checks and all(c[2]=='durable-dispatch' for c in checks)
assert len(set(c[2]['metadata']['_delivery_uuid'] for c in calls))==2
print(json.dumps(dict(passed=True,text_sends=1,file_sends=1,profile_checks=len(checks),model_turns=0,acknowledged=1)))
'''
    result = subprocess.run([str(runtime), '-c', code, str(host), str(engine), str(tmp_path/'home')],
                            env=dict(os.environ,HERMES_HOME=str(tmp_path/'home')),
                            capture_output=True,text=True,timeout=40)
    assert result.returncode == 0, result.stderr[-5000:]+result.stdout[-1000:]
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof['passed'] and proof['model_turns'] == 0 and proof['file_sends'] == 1
