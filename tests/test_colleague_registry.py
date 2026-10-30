import concurrent.futures
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "colleagues.sqlite", profile_id="team", primary_id="leader", primary_name="Lead")
    return registry, Actor("team", "leader"), Supervisor("team")


def role(registry, actor, role_id="review", key="role-1"):
    return registry.define_role(actor=actor, role_id=role_id, label=role_id.title(),
                                duty="Review supplied evidence", boundaries=["Read only"], request_id=key)


def hire(registry, actor, member_id="reviewer", role_id="review", key="hire-1"):
    return registry.hire(actor=actor, member_id=member_id, role_id=role_id,
                         display_name=member_id.title(), request_id=key)


def test_primary_is_permanently_protected_by_api_and_database(env):
    registry, leader, host = env
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.request_deactivation(actor=leader, member_id="leader", request_id="remove-leader")
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.finalize_deactivation(supervisor=host, member_id="leader", generation=1, external_idle=True)
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.grant_management(actor=leader, member_id="leader", enabled=False, request_id="remove-grant")
    with sqlite3.connect(registry.path) as db:
        for statement in ("DELETE FROM colleague_members WHERE member_id='leader'",
                          "UPDATE colleague_members SET status='inactive' WHERE member_id='leader'",
                          "UPDATE colleague_profiles SET primary_id='other' WHERE profile_id='team'",
                          "DELETE FROM colleague_profiles WHERE profile_id='team'",
                          "DELETE FROM colleague_roles WHERE role_id='primary'",
                          "INSERT OR REPLACE INTO colleague_profiles VALUES ('team','other')",
                          "INSERT OR REPLACE INTO colleague_members VALUES ('team','leader','primary','Lead','inactive',1,0)",
                          "INSERT OR REPLACE INTO colleague_roles VALUES ('team','primary','Other','Other','[]')"):
            with pytest.raises(sqlite3.IntegrityError, match="primary_protected"):
                db.execute(statement)
    assert registry.list_members(actor=leader)[0]["status"] == "active"
    with pytest.raises(ValueError, match="primary_identity_mismatch"):
        Registry(registry.path, profile_id="team", primary_id="other")


def test_delegated_recruiter_can_hire_and_authorize_successor(env):
    registry, leader, host = env
    role(registry, leader, "people", "define-people")
    hire(registry, leader, "recruiter", "people", "hire-recruiter")
    hr = Actor("team", "recruiter")
    with pytest.raises(PermissionError):
        role(registry, hr, "review", "denied-before-grant")
    registry.grant_management(actor=leader, member_id="recruiter", enabled=True, request_id="grant-recruiter")
    role(registry, hr, "review", "define-review")
    hire(registry, hr, "analyst", "review", "hire-analyst")
    hire(registry, hr, "recruiter_two", "people", "hire-recruiter-two")
    registry.grant_management(actor=hr, member_id="recruiter_two", enabled=True, request_id="grant-two")
    hr2 = Actor("team", "recruiter_two")
    hire(registry, hr2, "analyst_two", "review", "hire-another")
    assert {m["member_id"] for m in registry.list_members(actor=leader)} == {
        "leader", "recruiter", "recruiter_two", "analyst", "analyst_two"}
    review = next(x for x in registry.list_roles(actor=leader) if x["role_id"] == "review")
    assert review["duty"] == "Review supplied evidence" and review["boundaries"] == ["Read only"]
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.request_deactivation(actor=hr2, member_id="leader", request_id="remove-leader")
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.grant_management(actor=hr2, member_id="leader", enabled=False, request_id="strip-leader")
    assert registry.request_deactivation(actor=hr2, member_id="recruiter_two", request_id="remove-self")["status"] == "draining"
    assert registry.request_deactivation(actor=hr2, member_id="recruiter_two", request_id="remove-self")["replayed"]
    with pytest.raises(PermissionError):
        registry.define_role(actor=hr2, role_id="another", label="Another", duty="Review",
                             boundaries=["Read only"], request_id="new-after-retirement")
    registry.finalize_deactivation(supervisor=host, member_id="recruiter_two", generation=1, external_idle=True)
    assert registry.request_deactivation(actor=hr2, member_id="recruiter_two", request_id="remove-self")["replayed"]
    with pytest.raises(PermissionError):
        registry.request_deactivation(actor=hr2, member_id="recruiter_two", request_id="new-after-retirement")
    with pytest.raises(PermissionError):
        registry.grant_management(actor=Actor("team", "analyst"), member_id="analyst_two",
                                  enabled=True, request_id="unauthorized")


def test_active_member_reads_roster_and_roles_but_cannot_manage(env):
    registry, leader, host = env
    role(registry, leader)
    hire(registry, leader)
    colleague = Actor("team", "reviewer")
    roster = registry.list_members(actor=colleague)
    assert {entry["member_id"] for entry in roster} == {"leader", "reviewer"}
    description = next(entry for entry in registry.list_roles(actor=colleague) if entry["role_id"] == "review")
    assert description["duty"] == "Review supplied evidence" and description["boundaries"] == ["Read only"]
    first = registry.list_members(actor=colleague, limit=1)
    second = registry.list_members(actor=colleague, after=first[0]["member_id"], limit=1)
    assert len(first) == len(second) == 1 and first[0]["member_id"] != second[0]["member_id"]
    with pytest.raises(PermissionError, match="management_permission_required"):
        hire(registry, colleague, "other", "review", "unauthorized-hire")
    with pytest.raises(PermissionError, match="management_permission_required"):
        registry.grant_management(actor=colleague, member_id="reviewer", enabled=True, request_id="unauthorized-grant")
    with pytest.raises(PermissionError, match="management_permission_required"):
        registry.request_deactivation(actor=colleague, member_id="leader", request_id="unauthorized-retire")
    with pytest.raises(PermissionError, match="management_permission_required"):
        registry.history(actor=colleague)
    with pytest.raises(PermissionError):
        registry.list_members(actor=Actor("other", "reviewer"))
    registry.request_deactivation(actor=leader, member_id="reviewer", request_id="drain")
    with pytest.raises(PermissionError, match="active_member_required"):
        registry.list_members(actor=colleague)
    with pytest.raises(PermissionError, match="active_member_required"):
        registry.list_roles(actor=colleague)
    registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)
    with pytest.raises(PermissionError, match="active_member_required"):
        registry.list_roles(actor=colleague)
    assert registry.list_members(actor=leader)[0]["status"] == "active"


def test_actor_and_supervisor_are_host_bound_and_profile_scoped(env):
    registry, leader, _ = env
    role(registry, leader)
    for actor in (None, {"profile_id": "team", "member_id": "leader"}, Actor("other", "leader")):
        with pytest.raises(PermissionError):
            registry.hire(actor=actor, member_id="reviewer", role_id="review",
                          display_name="Reviewer", request_id="fake")
    hire(registry, leader)
    for host in (None, {"profile_id": "team"}, Supervisor("other")):
        with pytest.raises(PermissionError):
            registry.reserve_work(supervisor=host, member_id="reviewer", generation=1, work_id="job-1")
    other = Registry(registry.path, profile_id="other", primary_id="other_leader")
    with pytest.raises(PermissionError):
        other.list_members(actor=leader)


def test_isolated_file_loader_uses_its_own_host_bound_types(tmp_path):
    source = Path(__file__).resolve().parents[1] / "agentcore/colleague_registry.py"
    spec = importlib.util.spec_from_file_location("isolated_registry_loader", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = module.Registry(tmp_path / "isolated.sqlite", profile_id="team", primary_id="leader")
    assert registry.list_members(actor=module.Actor("team", "leader"))[0]["member_id"] == "leader"
    with pytest.raises(PermissionError):
        registry.list_members(actor=Actor("team", "leader"))


def test_replay_is_idempotent_and_identity_conflicts_fail_closed(env):
    registry, leader, _ = env
    first_role = role(registry, leader)
    assert not first_role["replayed"] and role(registry, leader)["replayed"]
    first = hire(registry, leader)
    replay = hire(registry, leader)
    assert first["created"] and replay["created"] and replay["replayed"]
    with pytest.raises(ValueError, match="request_id_conflict"):
        hire(registry, leader, "different", "review", "hire-1")
    with pytest.raises(ValueError, match="member_identity_conflict"):
        registry.hire(actor=leader, member_id="reviewer", role_id="review", display_name="Changed", request_id="new")
    events = registry.history(actor=leader)
    assert sum(x["action"] == "member_hired" for x in events) == 1
    assert sum(x["action"] == "role_defined" for x in events) == 1


def test_retirement_drains_tracked_work_preserves_history_and_runtime(env):
    registry, leader, host = env
    role(registry, leader)
    hire(registry, leader)
    member = next(x for x in registry.list_members(actor=leader) if x["member_id"] == "reviewer")
    assert member["status"] == "active" and member["runtime_state"] == "unknown"
    registry.observe_runtime(supervisor=host, member_id="reviewer", generation=1, state="running")
    registry.reserve_work(supervisor=host, member_id="reviewer", generation=1, work_id="job-1")
    assert registry.primary_member(supervisor=host) == "leader"
    before = registry.work_status(supervisor=host, member_id="reviewer", generation=1)
    assert before["tracked_open"] == 1 and before["coverage"] == "registry_reservations_only"
    requested = registry.request_deactivation(actor=leader, member_id="reviewer", request_id="remove")
    assert requested["status"] == "draining"
    assert registry.request_deactivation(actor=leader, member_id="reviewer", request_id="remove")["replayed"]
    with pytest.raises(ValueError, match="member_not_accepting_work"):
        registry.reserve_work(supervisor=host, member_id="reviewer", generation=1, work_id="job-2")
    with pytest.raises(ValueError, match="trusted_idle_observation_required"):
        registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=False)
    with pytest.raises(ValueError, match="active_work_remaining"):
        registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)
    registry.settle_work(supervisor=host, member_id="reviewer", generation=1, work_id="job-1")
    after = registry.work_status(supervisor=host, member_id="reviewer", generation=1)
    assert after["tracked_open"] == 0 and after["tracked_closed"] == 1
    assert registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)["status"] == "inactive"
    member = next(x for x in registry.list_members(actor=leader) if x["member_id"] == "reviewer")
    assert member["status"] == "inactive" and member["runtime_state"] == "running"
    assert registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)["status"] == "inactive"
    with pytest.raises(ValueError, match="settled_work_cannot_reopen"):
        registry.reserve_work(supervisor=host, member_id="reviewer", generation=1, work_id="job-1")
    assert [x["action"] for x in registry.history(actor=leader)][-3:] == [
        "deactivation_requested", "work_settled", "deactivation_finalized"]


def test_deactivate_and_work_admission_are_transactionally_fenced(tmp_path):
    path = tmp_path / "colleagues.sqlite"
    registry = Registry(path, profile_id="team", primary_id="leader")
    leader, host = Actor("team", "leader"), Supervisor("team")
    role(registry, leader)
    hire(registry, leader)
    other = Registry(path, profile_id="team", primary_id="leader")
    barrier = threading.Barrier(2)
    def reserve():
        barrier.wait(timeout=5)
        try:
            registry.reserve_work(supervisor=host, member_id="reviewer", generation=1, work_id="race")
            return "reserved"
        except ValueError as e:
            assert str(e) == "member_not_accepting_work"
            return "denied"
    def retire():
        barrier.wait(timeout=5)
        return other.request_deactivation(actor=leader, member_id="reviewer", request_id="retire")["status"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(reserve), pool.submit(retire)
        reserved, status = f1.result(timeout=10), f2.result(timeout=10)
    assert status == "draining"
    if reserved == "reserved":
        with pytest.raises(ValueError, match="active_work_remaining"):
            registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)
        registry.settle_work(supervisor=host, member_id="reviewer", generation=1, work_id="race")
    assert registry.finalize_deactivation(supervisor=host, member_id="reviewer", generation=1, external_idle=True)["status"] == "inactive"


def test_concurrent_identical_hiring_creates_one_member_and_one_event(tmp_path):
    path = tmp_path / "colleagues.sqlite"
    first = Registry(path, profile_id="team", primary_id="leader")
    second = Registry(path, profile_id="team", primary_id="leader")
    leader = Actor("team", "leader")
    role(first, leader)
    barrier = threading.Barrier(2)
    def submit(registry):
        barrier.wait(timeout=5)
        return hire(registry, leader, key="same-command")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1, f2 = pool.submit(submit, first), pool.submit(submit, second)
        results = [f1.result(timeout=10), f2.result(timeout=10)]
    assert {r["replayed"] for r in results} == {False, True}
    assert len([m for m in first.list_members(actor=leader) if m["member_id"] == "reviewer"]) == 1
    assert len([e for e in first.history(actor=leader) if e["action"] == "member_hired"]) == 1


def test_second_instance_initialization_never_drops_existing_sqlite_lock(tmp_path, monkeypatch):
    path = tmp_path / "colleagues.sqlite"
    registry = Registry(path, profile_id="team", primary_id="leader")
    holder = sqlite3.connect(path, timeout=1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    reached = threading.Event()
    original_db = Registry._db

    @contextmanager
    def monitored_db(self):
        if threading.current_thread().name == "second-registry":
            reached.set()
        with original_db(self) as db:
            yield db

    monkeypatch.setattr(Registry, "_db", monitored_db)
    errors = []
    def create_second():
        try:
            Registry(path, profile_id="team", primary_id="leader")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=create_second, name="second-registry")
    thread.start()
    try:
        assert reached.wait(5)
        script = """import sqlite3,sys
db=sqlite3.connect(sys.argv[1],timeout=0.3,isolation_level=None)
try:
 db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError:
 print('blocked')
else:
 print('UNEXPECTEDLY_ACQUIRED')
 db.rollback()
"""
        contender = subprocess.run([sys.executable, "-c", script, str(path)],
                                   capture_output=True, text=True, timeout=5, check=True)
        assert contender.stdout.strip() == "blocked"
    finally:
        holder.rollback()
        holder.close()
        thread.join(timeout=10)
    assert not thread.is_alive() and not errors
    assert registry.list_members(actor=Actor("team", "leader"))[0]["status"] == "active"


def test_role_description_is_bounded_descriptive_and_immutable(env):
    registry, leader, host = env
    role(registry, leader)
    hire(registry, leader)
    spec = registry.role_for_member(supervisor=host, member_id="reviewer", generation=1)
    assert spec["role_id"] == "review" and spec["duty"] == "Review supplied evidence"
    assert spec["boundaries"] == ["Read only"] and spec["status"] == "active"
    with pytest.raises(PermissionError):
        registry.role_for_member(supervisor={"profile_id": "team"}, member_id="reviewer", generation=1)
    with pytest.raises(ValueError, match="stale_member_generation"):
        registry.role_for_member(supervisor=host, member_id="reviewer", generation=2)
    with pytest.raises(ValueError, match="role_definition_conflict"):
        registry.define_role(actor=leader, role_id="review", label="Review", duty="Other duty",
                             boundaries=["Read only"], request_id="different-definition")
    with pytest.raises(ValueError, match="invalid_role_duty"):
        registry.define_role(actor=leader, role_id="other", label="Other", duty="bad\nline",
                             boundaries=["Read only"], request_id="invalid-duty")
    with pytest.raises(ValueError, match="invalid_role_boundaries"):
        registry.define_role(actor=leader, role_id="other", label="Other", duty="Review",
                             boundaries=[], request_id="invalid-boundary")
    assert len(registry.list_roles(actor=leader, limit=1)) == 1
    with pytest.raises(ValueError, match="invalid_role_window"):
        registry.list_roles(actor=leader, limit=201)


@pytest.mark.parametrize("bad", ["", "../x", "UPPER", "a" * 65, None, True])
def test_invalid_member_identifiers_rejected(env, bad):
    registry, leader, _ = env
    role(registry, leader)
    with pytest.raises(ValueError):
        registry.hire(actor=leader, member_id=bad, role_id="review", display_name="Name", request_id="one")


def test_exclusive_work_reservation_is_atomic_across_registry_instances(tmp_path):
    path = tmp_path / "exclusive.sqlite"
    registry = Registry(path, profile_id="team", primary_id="leader")
    other = Registry(path, profile_id="team", primary_id="leader")
    leader, host = Actor("team", "leader"), Supervisor("team")
    role(registry, leader); hire(registry, leader)
    barrier = threading.Barrier(2)
    def reserve(store, work_id):
        barrier.wait(timeout=5)
        try:
            result = store.reserve_work_exclusive(supervisor=host, member_id="reviewer",
                                                   generation=1, work_id=work_id)
            return ("reserved", result)
        except ValueError as exc:
            return ("denied", str(exc))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reserve, registry, "gateway-work")
        second = pool.submit(reserve, other, "cli-work")
        results = [first.result(timeout=10), second.result(timeout=10)]
    admitted = [value for kind,value in results if kind == "reserved"]
    denied = [value for kind,value in results if kind == "denied"]
    assert len(admitted) == len(denied) == 1
    assert denied == ["member_has_exclusive_work"]
    winner_id = admitted[0]["work_id"]
    replay = registry.reserve_work_exclusive(supervisor=host, member_id="reviewer",
                                               generation=1, work_id=winner_id)
    assert replay == {"work_id":winner_id,"reserved":True,"created":False}
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 1


def test_legacy_and_exclusive_reservations_are_fenced_in_both_orders(env):
    registry, leader, host = env
    role(registry, leader); hire(registry, leader)
    # Legacy queues retain their pre-existing multi-reservation behavior until an
    # exclusive execution enters the durable fence.
    registry.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-a")
    registry.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-b")
    with pytest.raises(ValueError,match="member_has_unresolved_work"):
        registry.reserve_work_exclusive(supervisor=host,member_id="reviewer",generation=1,work_id="exclusive-blocked")

    registry.settle_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-a")
    registry.settle_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-b")
    exclusive=registry.reserve_work_exclusive(supervisor=host,member_id="reviewer",generation=1,work_id="exclusive-one")
    assert exclusive["created"] is True
    other=Registry(registry.path,profile_id="team",primary_id="leader")
    with pytest.raises(ValueError,match="member_has_exclusive_work"):
        other.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-after-exclusive")
    with pytest.raises(ValueError,match="member_has_exclusive_work"):
        other.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="exclusive-one")

    # The fence survives Registry reconstruction and is released atomically with
    # settlement; repeated settlement is harmless, then legacy admission resumes.
    other.settle_work(supervisor=host,member_id="reviewer",generation=1,work_id="exclusive-one")
    other.settle_work(supervisor=host,member_id="reviewer",generation=1,work_id="exclusive-one")
    with other._db() as db:
        assert db.execute("SELECT 1 FROM colleague_exclusive_work WHERE profile_id=? AND member_id=?",
                          ("team","reviewer")).fetchone() is None
    assert other.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="legacy-after-close")["created"] is True

def test_legacy_and_exclusive_race_cannot_cross_the_durable_fence(tmp_path):
    path=tmp_path / "mixed-exclusive.sqlite"
    legacy=Registry(path,profile_id="team",primary_id="leader")
    exclusive=Registry(path,profile_id="team",primary_id="leader")
    leader,host=Actor("team","leader"),Supervisor("team")
    role(legacy,leader);hire(legacy,leader)
    barrier=threading.Barrier(2)
    def reserve(store, exclusive_path, work_id):
        barrier.wait(timeout=5)
        try:
            method=store.reserve_work_exclusive if exclusive_path else store.reserve_work
            return "won",method(supervisor=host,member_id="reviewer",generation=1,work_id=work_id)
        except ValueError as exc:
            return "lost",str(exc)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(reserve,legacy,False,"legacy-racer")
        b=pool.submit(reserve,exclusive,True,"exclusive-racer")
        outcomes=[a.result(timeout=10),b.result(timeout=10)]
    assert sum(kind=="won" for kind,_ in outcomes)==1
    assert sum(kind=="lost" for kind,_ in outcomes)==1
    loser=next(value for kind,value in outcomes if kind=="lost")
    assert loser in {"member_has_unresolved_work","member_has_exclusive_work"}
    assert legacy.work_status(supervisor=host,member_id="reviewer",generation=1)["tracked_open"]==1


def test_legacy_open_reservation_cannot_be_silently_upgraded_to_exclusive(env):
    registry, leader, host = env
    role(registry,leader);hire(registry,leader)
    registry.reserve_work(supervisor=host,member_id="reviewer",generation=1,work_id="same-id")
    with pytest.raises(ValueError,match="exclusive_work_unfenced_reservation"):
        registry.reserve_work_exclusive(supervisor=host,member_id="reviewer",generation=1,work_id="same-id")
    assert registry.work_status(supervisor=host,member_id="reviewer",generation=1)["tracked_open"]==1
    with registry._db() as db:
        assert db.execute("SELECT 1 FROM colleague_exclusive_work WHERE profile_id=? AND member_id=?",
                          ("team","reviewer")).fetchone() is None


def test_main_bot_leadership_and_anti_eviction_invariants(env):
    registry, leader, host = env

    # 1. Primary identification
    assert registry.is_primary_member("leader") is True
    assert registry.is_primary_member("other_member") is False

    # 2. Leadership assertions
    registry.assert_primary_leadership(actor=leader)

    role(registry, leader)
    hire(registry, leader)
    worker = Actor("team", "reviewer")
    with pytest.raises(PermissionError, match="primary_leadership_required"):
        registry.assert_primary_leadership(actor=worker)

    # 3. Anti-eviction invariants: primary member cannot be deactivated
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.request_deactivation(actor=leader, member_id="leader", request_id="deact-leader")

    with pytest.raises(PermissionError, match="primary_protected"):
        registry.finalize_deactivation(supervisor=host, member_id="leader", generation=1, external_idle=True)

    # 4. Anti-eviction invariants: primary member management authority cannot be revoked
    with pytest.raises(PermissionError, match="primary_protected"):
        registry.grant_management(actor=leader, member_id="leader", enabled=False, request_id="revoke-lead")

