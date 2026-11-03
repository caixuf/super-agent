"""Independent foreground/worker/delivery processes; synthetic model and IM only.

This proves normal process exit/recreation at durable boundaries, not an
in-flight crash, production service restart, real model or real IM acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from agentcore.pool import Pool


REPO = Path(__file__).resolve().parents[1]
ORIGIN = dict(profile="restart-profile", session="original-session", route="original-route",
              channel="test-im", chat="original-chat", thread="original-thread")


def test_dispatch_survives_foreground_worker_and_delivery_process_recreation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    source = home / "evidence.log"
    source.write_text("Synthetic restart evidence\n")
    (home / "config.yaml").write_text("superagent:\n  frontdesk:\n    enabled: true\n")
    (home / "policy.json").write_text(json.dumps({"allowed_roots": [str(home)]}))
    (home / "offline_model.py").write_text(
        "def analyze(messages):\n"
        "    return {'conclusion':'Evidence persisted across processes',"
        "'claims':[{'statement':'Evidence exists','evidence':[{'source_id':'source_1',"
        "'line':1,'quote':'Synthetic restart evidence'}]}],"
        "'limitations':['Synthetic offline model only']}\n"
    )
    runtime = [sys.executable, str(REPO / "agentcore/analysis_runner.py"),
               "--policy", str(home / "policy.json"), "--model-adapter", "offline_model:analyze"]
    config = home / "runner.json"
    config.write_text(json.dumps(runtime))
    queue = home / "queue"
    env = dict(os.environ, HOME=str(home), HERMES_HOME=str(home),
               PYTHONPATH=os.pathsep.join([str(home), str(REPO)]),
               PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1",
               HERMES_ENABLE_PROJECT_PLUGINS="0")
    for key in tuple(env):
        if key.startswith(("FEISHU_", "HFC_", "HERMES_SESSION_", "__HERMES_")) or key in (
                "HERMES_PROFILE", "HERMES_CONFIG", "HERMES_ENV", "HERMES_PYTHON_SRC_ROOT"):
            env.pop(key, None)

    def run(*args):
        proc = subprocess.run([sys.executable, *args], cwd=home, env=env,
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr[-3000:]
        return json.loads(proc.stdout.strip().splitlines()[-1])

    # The submitting process exits before any Worker starts. The request and
    # origin cannot survive via its Python objects or a live gateway context.
    foreground = run("-c", r'''
import importlib.util,json,pathlib,sys
home,repo=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2])
spec=importlib.util.spec_from_file_location("restart_dispatch",repo/"plugins/durable_dispatch/__init__.py")
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
origin=json.loads(sys.argv[3])
service=plugin.DispatchService(str(home/"queue"),origin["profile"],[str(home)],
    require_delivery_route=True,route_provider=lambda _:dict(origin))
args={"goal":"Analyze the evidence","request_key":"stable-request",
      "sources":[{"path":str(home/"evidence.log")} ]}
receipt=service.submit(origin["session"],args)
assert service.submit(origin["session"],args)["task_id"]==receipt["task_id"]
policy_spec=importlib.util.spec_from_file_location("superagent_frontdesk",repo/"runtime/hermes_frontdesk_policy.py")
policy=importlib.util.module_from_spec(policy_spec);sys.modules[policy_spec.name]=policy;policy_spec.loader.exec_module(policy)
turn_spec=importlib.util.spec_from_file_location("restart_turn",repo/"runtime/hermes_frontdesk_turn.py")
turn=importlib.util.module_from_spec(turn_spec);turn_spec.loader.exec_module(turn)
answer=turn.release_after_submission(["submit"],[{"role":"tool","name":"durable_submit",
    "tool_call_id":"submit","content":json.dumps(receipt)}],home=home)
assert receipt["task_id"] in answer
print(json.dumps({"task_id":receipt["task_id"],"foreground_pid":__import__("os").getpid()}))
''', str(home), str(REPO), json.dumps(ORIGIN))
    job = foreground["task_id"]
    assert Pool(queue).status(job)["status"] == "queued"

    command = ["-m", "agentcore.pool", "--root", str(queue), "worker",
               "--runner-config", str(config), "--workers", "1", "--drain"]
    first = run(*command)
    assert first["attempts_finished"] == 1
    pool = Pool(queue)
    status = pool.status(job)
    assert status["status"] == "completed" and status["tries"] == 1, status
    result = json.loads(status["result"])["result"]
    assert result["verification"] == "citation_traceability_only"
    assert all(Path(path).is_file() for path in result["artifacts"])
    assert pool.origin(job) == ORIGIN
    assert pool.delivery_record(job)["acknowledged"] == 0

    # A new Worker process must not rerun completed work while delivery is still
    # pending. This is a normal restart boundary, not a stopped-descendant proof.
    assert run(*command)["attempts_finished"] == 0
    assert Pool(queue).status(job)["tries"] == 1

    delivery_program = r'''
import json,os,pathlib,sys
from agentcore.pool import Pool
from agentcore.completion_backfill import drain
from agentcore.ops_alerts import DeliveryReceipt
home=pathlib.Path(sys.argv[1]);origin=json.loads(sys.argv[2]);job=sys.argv[3]
pool=Pool(home/"queue");calls=[]
class Transport:
    def deliver(self,address,message,*,idempotency_key):
        assert address==origin and idempotency_key==job
        assert message["task_id"]==job and message["status"]=="completed"
        calls.append(job)
        return DeliveryReceipt(True,idempotency_key,"synthetic-platform-receipt")
transport=Transport()
assert drain(pool,transport,profile="other-profile")["delivered"]==0 and calls==[]
result=drain(pool,transport,profile=origin["profile"])
print(json.dumps({"pid":os.getpid(),"delivered":result["delivered"],"calls":calls,
    "acknowledged":pool.delivery_record(job)["acknowledged"]}))
'''
    delivery = run("-c", delivery_program, str(home), json.dumps(ORIGIN), job)
    assert delivery["delivered"] == 1 and delivery["calls"] == [job]
    assert delivery["acknowledged"] == 1
    restarted = run("-c", delivery_program, str(home), json.dumps(ORIGIN), job)
    assert restarted["delivered"] == 0 and restarted["calls"] == []
    assert restarted["acknowledged"] == 1
    assert len({os.getpid(), foreground["foreground_pid"], delivery["pid"], restarted["pid"]}) == 4
    assert Pool(queue).delivery_record(job)["receipt_id"] == "synthetic-platform-receipt"
