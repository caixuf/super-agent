"""Trusted host binding: same team facts, separate profile event inboxes."""
import os
import json
import json
from pathlib import Path
import sys
import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.colleague_shared_memory import ColleagueMemoryBindingStore, configure_memory_from_binding_file


def _store(tmp, team, members):
    registry=Registry(tmp/f"{team}.db",profile_id=team,primary_id="leader")
    actor=Actor(team,"leader"); supervisor=Supervisor(team)
    registry.define_role(actor=actor,role_id="general",label="General",duty="General duties",
                         boundaries=["Host owns access"],request_id="role")
    mapping={"leader_profile":("leader",0)}
    for member,profile in members:
        registry.hire(actor=actor,member_id=member,role_id="general",display_name=member,
                      request_id="hire-"+member)
        mapping[profile]=(member,1)
    root=tmp/(team+"-private"); root.mkdir(mode=0o700); os.chmod(root,0o700)
    return ColleagueMemoryBindingStore(registry=registry,supervisor=supervisor,
                                       profile_members=mapping,state_dir=root)


def _seed(store,profile,statement):
    binding=store.resolve(profile=profile,session_id="seed-session")
    configure_memory_from_binding_file(store.binding_file,profile)
    from agentcore.memory.config import MemoryConfig,configure
    configure(MemoryConfig(data_dir=binding.data_dir,default_user=binding.user_id,
        default_agent=binding.agent_id,embed_dim=64,
        embed_fn=lambda texts:[[1.0]+[0.0]*63 for _ in texts]))
    from agentcore.memory import memory_store
    result=memory_store.write_fact(statement,subject="team decision",user_id=binding.user_id,
                                   agent_id=binding.agent_id,source="manual")
    assert result["ok"] is True
    assert memory_store.flush_background(timeout=30)==0


def _provider():
    try:
        from plugins.memory import _load_provider_from_dir
        root = Path(__file__).resolve().parents[1]
        instance = _load_provider_from_dir(root / "plugins" / "localvec", register_skills=False)
        if instance is not None:
            return instance, sys.modules[instance.__class__.__module__]
    except Exception:
        pass
    import types
    if "agent.memory_provider" not in sys.modules:
        provider_mod = types.ModuleType("agent.memory_provider")
        provider_mod.MemoryProvider = object
        provider_mod.RecallStatus = lambda **kw: types.SimpleNamespace(**kw)
        provider_mod.is_trivial_prompt = lambda query: False
        import threading
        provider_mod.spawn_context_thread = lambda target, *, name, args=(), **kw: threading.Thread(
            target=target, name=name, args=args, daemon=True)
        sys.modules["agent.memory_provider"] = provider_mod
    root = Path(__file__).resolve().parents[1]
    if "plugins.localvec" in sys.modules:
        module = sys.modules["plugins.localvec"]
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("plugins.localvec", root / "plugins/localvec/__init__.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["plugins.localvec"] = module
        spec.loader.exec_module(module)
    instance = module.LocalVecProvider()
    return instance, module


def test_host_manager_receives_unavailable_before_its_timeout(monkeypatch):
    import threading
    import pytest
    manager_module = pytest.importorskip('agent.memory_manager')
    provider, module = _provider()
    provider._cmd = ['configured']; provider._timeout = 2
    monkeypatch.setattr(module, 'MAX_FOREGROUND_WAIT_S', .1)
    entered = threading.Event(); release = threading.Event(); finished = threading.Event()
    def slow_recall(query, key):
        entered.set(); release.wait(3); finished.set(); return True
    monkeypatch.setattr(provider, '_recall', slow_recall)
    manager = manager_module.MemoryManager(external_prefetch_timeout=1)
    try:
        result = manager._prefetch_provider(provider, 'query', session_id='session-a')
        assert entered.is_set() and not finished.is_set()
        assert result == module.RECALL_UNAVAILABLE
    finally:
        release.set(); assert finished.wait(3)


def test_team_share_session_separation_and_cross_team_isolation(tmp_path,monkeypatch):
    a=_store(tmp_path,"team_alpha",[("alice","alice_profile"),("bob","bob_profile")])
    b=_store(tmp_path,"team_beta",[("carol","carol_profile")])
    alice=a.resolve(profile="alice_profile",session_id="session-a")
    bob=a.resolve(profile="bob_profile",session_id="session-b")
    carol=b.resolve(profile="carol_profile",session_id="session-c")
    assert alice.data_dir==bob.data_dir
    assert alice.user_id==bob.user_id=="team_alpha"
    assert alice.agent_id=="alice" and bob.agent_id=="bob"
    assert alice.spool_dir!=bob.spool_dir and alice.queue_path!=bob.queue_path
    assert carol.data_dir!=alice.data_dir and carol.user_id=="team_beta"

    from agentcore.memory.turn_spool import TurnSpool
    s1=a.resolve(profile="alice_profile",session_id="session-a")
    s2=a.resolve(profile="alice_profile",session_id="session-a2")
    for bound,turn,text in ((s1,"turn-1","private context one"),(s2,"turn-2","private context two")):
        spool=TurnSpool(bound.spool_dir,profile=bound.profile,user_id=bound.user_id,agent_id=bound.agent_id)
        spool.capture(session_id=bound.session_id,turn_id=turn,user_message=text,assistant_reply="private reply")
    records=[json.loads(x.read_text()) for x in s1.spool_dir.glob("*.json")]
    assert {r["session_id"] for r in records}=={"session-a","session-a2"}
    assert {r["user_message"] for r in records}=={"private context one","private context two"}

    monkeypatch.setenv("MEMORY_OLLAMA_BASE_URL","http://127.0.0.1:1")
    _seed(a,"alice_profile","Team alpha decision marker ALPHA_SHARED_731.")
    _seed(b,"carol_profile","Team beta decision marker BETA_PRIVATE_942.")
    provider,module=_provider()
    root=Path(__file__).resolve().parents[1]
    config=a.provider_config(profile="alice_profile",python=sys.executable,repo_root=root)
    monkeypatch.setattr(module,"_config_block",lambda: config|{"limit":6,"timeout_s":20})
    provider.initialize("session-a")
    assert "ALPHA_SHARED_731" in provider.prefetch("ALPHA_SHARED_731",session_id="session-a")

    sibling,_=_provider()
    config=a.provider_config(profile="bob_profile",python=sys.executable,repo_root=root)
    monkeypatch.setattr(module,"_config_block",lambda: config|{"limit":6,"timeout_s":20})
    sibling.initialize("session-b")
    assert "ALPHA_SHARED_731" in sibling.prefetch("ALPHA_SHARED_731",session_id="session-b")

    outsider,_=_provider()
    config=b.provider_config(profile="carol_profile",python=sys.executable,repo_root=root)
    monkeypatch.setattr(module,"_config_block",lambda: config|{"limit":6,"timeout_s":20})
    outsider.initialize("session-c")
    got=outsider.prefetch("ALPHA_SHARED_731",session_id="session-c")
    assert "ALPHA_SHARED_731" not in got and "BETA_PRIVATE_942" not in got


def test_host_worker_binding_fixes_team_identity_and_private_queue_paths(tmp_path):
    store=_store(tmp_path,"team_gamma",[("alice","alice_profile"),("bob","bob_profile")])
    alice=store.worker_binding(profile="alice_profile")
    bob=store.worker_binding(profile="bob_profile")
    assert alice["bootstrap"]=="agentcore.colleague_shared_memory:configure_worker_from_env"
    assert alice["user_id"]==bob["user_id"]=="team_gamma"
    assert alice["data_dir"]==bob["data_dir"]
    assert alice["agent_id"]=="alice" and bob["agent_id"]=="bob"
    assert alice["spool_dir"]!=bob["spool_dir"] and alice["queue_path"]!=bob["queue_path"]
    assert alice["environment"]["AGENTCORE_COLLEAGUE_MEMORY_PROFILE"]=="alice_profile"
    bridge=store.memory_bridge_settings(profile="alice_profile",engine_root=Path(__file__).resolve().parents[1])
    assert bridge["user_id"]==alice["user_id"] and bridge["agent_id"]=="alice"
    assert bridge["spool_dir"]==alice["spool_dir"]


def test_binding_fails_closed_for_unknown_profile_and_wrong_primary_generation(tmp_path):
    store=_store(tmp_path,"team_delta",[("alice","alice_profile")])
    with pytest.raises(PermissionError,match="profile_not_bound"):
        store.resolve(profile="other",session_id="s")
    with pytest.raises(ValueError,match="invalid_primary_memory_generation"):
        ColleagueMemoryBindingStore(registry=store.registry,supervisor=store.supervisor,
            profile_members={"bad":("leader",1)},state_dir=store.state_dir)



def test_same_team_binding_map_is_locked_and_conflicting_profile_sets_refused(tmp_path):
    first=_store(tmp_path,"team_epsilon",[("alice","alice_profile")])
    # A second host bootstrap with the exact same trusted mapping is idempotent.
    identical=ColleagueMemoryBindingStore(registry=first.registry,supervisor=first.supervisor,
        profile_members={"leader_profile":("leader",0),"alice_profile":("alice",1)},
        state_dir=first.state_dir)
    assert identical.binding_file==first.binding_file
    with pytest.raises(ValueError,match="shared_team_memory_binding_conflict"):
        ColleagueMemoryBindingStore(registry=first.registry,supervisor=first.supervisor,
            profile_members={"leader_profile":("leader",0),"bob_profile":("alice",1)},
            state_dir=first.state_dir)
    assert json.loads(first.binding_file.read_text())["profiles"]["alice_profile"]["member_id"]=="alice"



def test_real_runtime_service_ingests_turn_to_profile_bound_team_queue(tmp_path):
    import subprocess
    import sys
    store=_store(tmp_path,"team_zeta",[("alice","alice_profile"),("bob","bob_profile")])
    bound=store.resolve(profile="alice_profile",session_id="alice-session")
    from agentcore.memory.turn_spool import TurnSpool
    spool=TurnSpool(bound.spool_dir,profile=bound.profile,user_id=bound.user_id,agent_id=bound.agent_id)
    spool.capture(session_id=bound.session_id,turn_id="turn-zeta",
                  user_message="a completed isolated fixture turn",assistant_reply="fixture reply")
    assert spool.finalize(session_id=bound.session_id,turn_id="turn-zeta",completed=True)=="completed"
    command=store.worker_command(profile="alice_profile",python=sys.executable,
                                 repo_root=Path(__file__).resolve().parents[1])
    env=os.environ.copy()
    env.update(command["environment"])
    env.pop("OPENAI_API_KEY",None)
    env.pop("OPENAI_BASE_URL",None)
    result=subprocess.run(command["argv"]+["--once"],cwd=command["cwd"],env=env,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    from agentcore.memory.runtime_bridge import TurnEventQueue
    queue=TurnEventQueue(bound.queue_path)
    with queue._connect() as db:
        event=db.execute("SELECT profile,user_id,agent_id,session_id,status FROM turn_events").fetchone()
    assert event is not None
    assert event["profile"]=="alice_profile" and event["user_id"]=="team_zeta"
    assert event["agent_id"]=="alice" and event["session_id"]=="alice-session"
    bob=store.worker_binding(profile="bob_profile")
    assert bob["queue_path"]!=command["argv"][command["argv"].index("--queue")+1]



def test_profile_binding_rechecks_current_registry_generation_and_active_status(tmp_path):
    store=_store(tmp_path,"team_theta",[("alice","alice_profile")])
    from agentcore.colleague_registry import Actor
    actor=Actor("team_theta","leader")
    store.registry.request_deactivation(actor=actor,member_id="alice",request_id="retire-alice")
    with pytest.raises(PermissionError,match="memory_member_not_active"):
        configure_memory_from_binding_file(store.binding_file,"alice_profile")



def test_explicit_binding_refresh_adds_member_but_never_rebinds_or_removes_profile(tmp_path):
    store=_store(tmp_path,"team_eta",[("alice","alice_profile")])
    revision=store.binding_revision
    actor=Actor("team_eta","leader")
    store.registry.hire(actor=actor,member_id="bob",role_id="general",display_name="bob",
                        request_id="hire-bob")
    store.registry.hire(actor=actor,member_id="carol",role_id="general",display_name="carol",
                        request_id="hire-carol")
    profiles={"leader_profile":("leader",0),"alice_profile":("alice",1),"bob_profile":("bob",1)}
    next_revision=store.refresh_profile_bindings(profiles,expected_revision=revision)
    assert next_revision!=revision
    assert store.resolve(profile="bob_profile",session_id="bob-s").user_id=="team_eta"
    with pytest.raises(RuntimeError,match="stale_memory_binding_revision"):
        store.refresh_profile_bindings(profiles,expected_revision=revision)
    with pytest.raises(PermissionError,match="profile_rebind_or_removal"):
        store.refresh_profile_bindings(
            {"leader_profile":("leader",0),"bob_profile":("bob",1)},
            expected_revision=next_revision)
    with pytest.raises(PermissionError,match="profile_rebind_or_removal"):
        store.refresh_profile_bindings(
            {"leader_profile":("leader",0),"alice_profile":("carol",1),"bob_profile":("bob",1)},
            expected_revision=next_revision)
    assert store.resolve(profile="alice_profile",session_id="alice-s").member_id=="alice"



def test_private_directory_guard_never_chmods_symlink_target(tmp_path):
    target=tmp_path/"public"
    target.mkdir(mode=0o755)
    os.chmod(target,0o755)
    link=tmp_path/"teams"
    link.symlink_to(target,target_is_directory=True)
    before=target.stat().st_mode
    with pytest.raises(ValueError,match="symlink_or_not_directory"):
        ColleagueMemoryBindingStore._ensure_private_dir(link,tmp_path)
    assert target.stat().st_mode==before


def test_host_supervises_the_memory_runtime_and_stops_it_cleanly(tmp_path):
    import sys
    from agentcore import colleague_containment as containment
    store=_store(tmp_path,"team_iota",[("alice","alice_profile")])
    supervisor=store.runtime_supervisor(profile="alice_profile",python=sys.executable,
                                        repo_root=Path(__file__).resolve().parents[1])
    started=supervisor.start()
    try:
        assert supervisor.running() is True
        assert supervisor.ready(timeout=25), supervisor.log_path.read_text()[-2000:]
        assert store.worker_binding(profile="alice_profile")["queue_path"] in \
            supervisor._command["argv"]
        with pytest.raises(RuntimeError,match="memory_runtime_already_running"):
            supervisor.start()
    finally:
        stopped=supervisor.stop()
    assert stopped["returncode"]==0
    assert stopped["quiescent"] is False
    assert stopped["quiescence_evidence_id"] is None
    assert supervisor.running() is False
    assert containment.tagged(started["token"])==[]
    assert supervisor.log_path.stat().st_mode&0o077==0


def test_runtime_stop_without_start_fails_closed(tmp_path):
    import sys
    store=_store(tmp_path,"team_kappa",[("alice","alice_profile")])
    supervisor=store.runtime_supervisor(profile="alice_profile",python=sys.executable,
                                        repo_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RuntimeError,match="memory_runtime_not_started"):
        supervisor.stop()
    with pytest.raises(PermissionError,match="profile_not_bound"):
        store.runtime_supervisor(profile="other",python=sys.executable,
                                 repo_root=Path(__file__).resolve().parents[1])
