import json
import os
import signal
import threading
import time
from contextlib import suppress

import pytest

from agentcore.colleague_registry import Actor, Registry, Supervisor
from agentcore.colleague_hermes_runner import (
    HermesColleagueRunner, HermesExecutionError, HermesSessionBindingStore,
    HermesSessionCreationPending, ProcessResult, ProfileBinding, load_profile_bindings,
)


def QUIESCED(code, stdout, stderr, *, timed_out=False, token="0" * 32):
    """A launcher result that proves the whole attempt is gone.

    ``ProcessResult`` now requires that proof: a launcher which cannot show the
    attempt is empty must leave ``quiescent`` False and the turn is not settled.
    """
    return ProcessResult(code, stdout, stderr, timed_out=timed_out, quiescent=True,
                         containment={"token": token, "signalled": [], "survivors": [],
                                      "quiescent": True, "proof_scope": "whole_attempt"})


class Ready:
    def status(self, member_id, generation):
        return {"ready": True, "member_id": member_id, "generation": generation}


@pytest.fixture
def context(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite", profile_id="team", primary_id="leader")
    leader, host = Actor("team", "leader"), Supervisor("team")
    registry.define_role(actor=leader, role_id="review", label="Reviewer",
                         duty="Review supplied work", boundaries=["Do not alter files"],
                         request_id="role")
    registry.hire(actor=leader, member_id="reviewer", role_id="review",
                  display_name="Reviewer", request_id="hire")
    exe = tmp_path / "hermes"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o700)
    state = tmp_path / "runner-state"
    state.mkdir(mode=0o700)
    return registry, host, exe, state


def _binding(owner, session, profile="reviewer_profile"):
    return ProfileBinding("reviewer", 1, owner[0], owner[1], profile, session)


def _runner(registry, host, exe, state, launcher, bindings=None):
    if bindings is None:
        owner = ("main", "source-session")
        bindings = {( "reviewer", 1, *owner): _binding(owner, "local-49023aa89ec4b3b6f5f830030a4f78f6367d196fbad2bd5597ac14cc61492561")}
    return HermesColleagueRunner(
        registry=registry, supervisor=host, provisioner=Ready(),
        bindings=bindings, executable=exe, state_dir=state, launcher=launcher,
    )


def _run(runner, request_id="req-1", owner_profile="main", owner_session="source-session"):
    return runner.run(member_id="reviewer", generation=1, request_id=request_id,
                      prompt="Inspect this patch", owner_profile=owner_profile,
                      owner_session=owner_session)


def test_runner_uses_host_binding_and_returns_assignment_identity(context):
    registry, host, exe, state = context
    seen = []

    def launch(argv, *, input, timeout):
        seen.append((argv, input, timeout))
        return QUIESCED(0, "review complete\n(session_id=debug)", "")

    runner = _runner(registry, host, exe, state, launch)
    result = _run(runner)
    argv, query, timeout = seen[0]
    assert argv == [str(exe), "--profile", "reviewer_profile", "chat",
                    "--query-file", "-", "--oneshot", "-Q", "--resume", "local-49023aa89ec4b3b6f5f830030a4f78f6367d196fbad2bd5597ac14cc61492561"]
    assert all("Inspect this patch" not in arg for arg in argv)
    assert "Inspect this patch" in query
    assert "member_id=reviewer; generation=1" in query
    assert "Review supplied work" in query
    assert "Do not alter files" not in query
    assert "general Hermes capabilities or tools" in query
    assert timeout == 180
    assert result["requested_by"] == {"profile": "main", "session": "source-session"}
    assert result["executed_by"] == {
        "member_id": "reviewer", "generation": 1,
        "hermes_profile": "reviewer_profile",
        "hermes_session_id": "local-49023aa89ec4b3b6f5f830030a4f78f6367d196fbad2bd5597ac14cc61492561"}
    assert result["cli_stdout_is_parsed_answer"] is False
    assert "session_id=debug" in result["cli_stdout_unparsed"]
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0


def test_distinct_owner_conversations_use_distinct_hermes_sessions(context):
    registry, host, exe, state = context
    seen = []
    bindings = {
        ("reviewer", 1, "main", "user-a"): _binding(("main", "user-a"), "local-608782e8706a4bb24c3e5a865788542b15720024c509abf2abaac6468510bc3e"),
        ("reviewer", 1, "main", "user-b"): _binding(("main", "user-b"), "local-001d7822bf860d6892def7ecf53e6433dfab969046c183d4aa71bfbca6c70311"),
    }

    def launch(argv, *, input, timeout):
        seen.append(argv)
        return QUIESCED(0, "ok", "")

    runner = _runner(registry, host, exe, state, launch, bindings)
    a = _run(runner, "a-1", "main", "user-a")
    b = _run(runner, "b-1", "main", "user-b")
    assert seen[0][2] == seen[1][2] == "reviewer_profile"
    assert seen[0][seen[0].index("--resume") + 1] == "local-608782e8706a4bb24c3e5a865788542b15720024c509abf2abaac6468510bc3e"
    assert seen[1][seen[1].index("--resume") + 1] == "local-001d7822bf860d6892def7ecf53e6433dfab969046c183d4aa71bfbca6c70311"
    assert a["requested_by"]["session"] == "user-a"
    assert b["requested_by"]["session"] == "user-b"
    assert a["executed_by"]["hermes_session_id"] != b["executed_by"]["hermes_session_id"]


def test_same_hermes_session_is_single_writer(context):
    registry, host, exe, state = context
    entered, release = threading.Event(), threading.Event()
    active = 0
    max_active = 0
    guard = threading.Lock()

    def launch(argv, *, input, timeout):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        entered.set()
        assert release.wait(3)
        with guard:
            active -= 1
        return QUIESCED(0, "ok", "")

    runner = _runner(registry, host, exe, state, launch)
    errors = []
    first = threading.Thread(target=lambda: _run(runner, "same-1"))
    second = threading.Thread(target=lambda: _run(runner, "same-2"))
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.15)
    assert max_active == 1
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert max_active == 1
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0


def test_runner_rejects_stale_or_unprovisioned_member_before_launch(context):
    registry, host, exe, state = context
    calls = []
    runner = _runner(registry, host, exe, state, lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="stale_member_generation"):
        runner.run(member_id="reviewer", generation=2, request_id="req-2", prompt="Do work",
                   owner_profile="main", owner_session="source-session")
    assert calls == []

    class NotReady:
        def status(self, member_id, generation):
            return {"ready": False}
    runner.provisioner = NotReady()
    with pytest.raises(RuntimeError, match="member_not_ready"):
        _run(runner, "req-3")
    assert calls == []
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0


def test_unknown_timeout_and_launcher_exception_keep_reservation_and_quarantine(context):
    registry, host, exe, state = context
    runner = _runner(registry, host, exe, state,
                     lambda *a, **k: QUIESCED(-9, "", "", timed_out=True))
    with pytest.raises(HermesExecutionError, match="reservation retained"):
        _run(runner, "timeout-1")
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 1
    with pytest.raises(RuntimeError, match="member_has_unresolved_work"):
        _run(runner, "timeout-2")

    with pytest.raises(PermissionError, match="explicit_quiescence"):
        runner.reconcile_uncertain(member_id="reviewer", generation=1, request_id="timeout-1",
                                   owner_profile="main", owner_session="source-session",
                                   process_quiesced=True, side_effects_reviewed=False)
    resolved = runner.reconcile_uncertain(
        member_id="reviewer", generation=1, request_id="timeout-1",
        owner_profile="main", owner_session="source-session",
        process_quiesced=True, side_effects_reviewed=True)
    assert resolved["retry_permitted"] is False
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0

    def launcher_error(*args, **kwargs):
        raise OSError("unknown launch outcome")
    runner.launcher = launcher_error
    with pytest.raises(HermesExecutionError, match="reservation retained"):
        _run(runner, "unknown-1")
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 1


def test_nonzero_exit_is_uncertain_and_reservation_is_retained(context):
    registry, host, exe, state = context
    calls = []

    def fail(argv, *, input, timeout):
        calls.append(argv)
        return QUIESCED(9, "", "provider details")

    runner = _runner(registry, host, exe, state, fail)
    with pytest.raises(HermesExecutionError, match="reservation retained"):
        _run(runner, "nonreplayable")
    with pytest.raises(RuntimeError, match="member_has_unresolved_work"):
        _run(runner, "another-request")
    assert len(calls) == 1
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 1


def test_binding_loader_requires_owner_only_and_pins_owner_and_generation(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps([{
        "member_id": "reviewer", "generation": 2, "owner_profile": "main",
        "owner_session": "source-a", "hermes_profile": "reviewer_v2",
        "hermes_session_id": "local-6ef5059d8797c4d40f6d251fb48f3ced0bc8875f98f8c21a81747eeca3419b06",
    }]))
    path.chmod(0o600)
    bindings = load_profile_bindings(path)
    assert bindings[("reviewer", 2, "main", "source-a")] == ProfileBinding(
        "reviewer", 2, "main", "source-a", "reviewer_v2", "local-6ef5059d8797c4d40f6d251fb48f3ced0bc8875f98f8c21a81747eeca3419b06")

    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_binding_file_required"):
        load_profile_bindings(path)


def test_binding_loader_rejects_shared_session_or_injected_profile(tmp_path):
    path = tmp_path / "bindings.json"
    rows = [
        {"member_id": "reviewer", "generation": 1, "owner_profile": "main",
         "owner_session": "a", "hermes_profile": "reviewer", "hermes_session_id": "local-457b24cde4770d16d40dab192b31924db5f0f2a5d481915a2498487f6233b94b"},
        {"member_id": "reviewer", "generation": 1, "owner_profile": "main",
         "owner_session": "b", "hermes_profile": "reviewer", "hermes_session_id": "local-457b24cde4770d16d40dab192b31924db5f0f2a5d481915a2498487f6233b94b"},
    ]
    path.write_text(json.dumps(rows))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="duplicate_binding"):
        load_profile_bindings(path)
    rows[0]["hermes_profile"] = "../../leader"
    path.write_text(json.dumps([rows[0]]))
    with pytest.raises(ValueError, match="invalid_binding_entry"):
        load_profile_bindings(path)


def test_installed_hermes_parser_accepts_stdin_oneshot_with_exact_session_id():
    import os
    import subprocess
    from pathlib import Path

    source = Path(os.environ.get("HERMES_SOURCE", Path.home() / ".hermes" / "hermes-agent"))
    python = source / "venv" / "bin" / "python"
    if not (source / "hermes_cli" / "_parser.py").is_file() or not python.is_file():
        pytest.skip("Hermes source and interpreter unavailable")
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from hermes_cli._parser import build_top_level_parser; "
        "p,_,_=build_top_level_parser(); "
        "a=p.parse_args(['chat','--query-file','-','--oneshot','-Q','--resume',"
        "'local-49023aa89ec4b3b6f5f830030a4f78f6367d196fbad2bd5597ac14cc61492561']); "
        "assert (a.command,a.query_file,a.oneshot_exit,a.quiet,a.resume)=="
        "('chat','-',True,True,'local-49023aa89ec4b3b6f5f830030a4f78f6367d196fbad2bd5597ac14cc61492561')"
    ) % str(source)
    subprocess.run([str(python), "-c", code], check=True, capture_output=True, text=True)


def test_distinct_hermes_sessions_share_member_generation_single_writer(context):
    registry, host, exe, state = context
    entered, release = threading.Event(), threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0
    seen = []
    bindings = {
        ("reviewer", 1, "main", "owner-a"): _binding(
            ("main", "owner-a"), "local-c5014096845ee0cd08991ca78db321765ed9f80f0a1df1e268545478a6878dbd"),
        ("reviewer", 1, "main", "owner-b"): _binding(
            ("main", "owner-b"), "local-1219d572ca224315ef169728223cf5805ab541393c3b54529801d940d70c94d1"),
    }

    def launch(argv, *, input, timeout):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            seen.append(argv[argv.index("--resume") + 1])
        entered.set()
        assert release.wait(3)
        with guard:
            active -= 1
        return QUIESCED(0, "ok", "")

    runner = _runner(registry, host, exe, state, launch, bindings)
    errors = []

    def invoke(owner, request):
        try:
            _run(runner, request, "main", owner)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=invoke, args=("owner-a", "owner-a-task"))
    second = threading.Thread(target=invoke, args=("owner-b", "owner-b-task"))
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.15)
    assert max_active == 1
    release.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert max_active == 1
    assert set(seen) == {"local-c5014096845ee0cd08991ca78db321765ed9f80f0a1df1e268545478a6878dbd", "local-1219d572ca224315ef169728223cf5805ab541393c3b54529801d940d70c94d1"}
    assert registry.work_status(supervisor=host, member_id="reviewer", generation=1)["tracked_open"] == 0


def test_launch_ends_a_descendant_that_holds_the_output_pipes(tmp_path, monkeypatch):
    """A detached child inheriting stdout must not stall the attempt after a timeout."""
    from agentcore.colleague_hermes_runner import _launch
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "leaky_hermes"
    script.write_text("#!/bin/sh\nsetsid sleep 60 &\necho $! > \"$PIDFILE\"\nsleep 60\n")
    script.chmod(0o755)
    monkeypatch.setenv("PIDFILE", str(pidfile))
    result = _launch([str(script)], input="", timeout=0.5)
    assert result.timed_out is True
    assert result.quiescent is False
    assert result.containment["survivors"] == []
    assert int(pidfile.read_text()) in result.containment["signalled"]
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_launch_claims_client_scope_when_the_turn_runs_elsewhere(tmp_path, monkeypatch):
    """An empty sweep is not containment when the turn runs outside this process.

    ``hermes chat`` submits the turn to a long-lived gateway; the gateway starts
    the tools' processes, and they never inherit the attempt environment. This
    launcher therefore reports ``cli_client`` scope, so a caller must not settle
    work on the empty sweep alone.
    """
    from agentcore.colleague_hermes_runner import _launch

    pidfile = tmp_path / "delegatee.pid"
    script = tmp_path / "gateway_delegating_hermes"
    # ``env -i`` mirrors a gateway child: it does not inherit the attempt token.
    # 用绝对路径调 setsid/sleep：``env -i`` 会清空 PATH，靠 PATH 解析的可执行文件
    # 在精简镜像里会直接退出，pid 记下来时进程已经死了（CI 上就是这样红的）。
    # setsid 缺失时退化为普通后台进程 —— 两种情形都不继承环境，用例要证的东西不变。
    script.write_text(
        "#!/bin/sh\n"
        "SLEEP_BIN=/bin/sleep; [ -x \"$SLEEP_BIN\" ] || SLEEP_BIN=/usr/bin/sleep\n"
        "if command -v setsid >/dev/null 2>&1; then\n"
        "  env -i setsid \"$SLEEP_BIN\" 300 >/dev/null 2>&1 &\n"
        "else\n"
        "  env -i \"$SLEEP_BIN\" 300 >/dev/null 2>&1 &\n"
        "fi\n"
        "echo $! > \"$PIDFILE\"\n"
        "echo 'turn complete'\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PIDFILE", str(pidfile))
    result = _launch([str(script)], input="", timeout=10)
    delegatee = int(pidfile.read_text())
    try:
        assert result.returncode == 0
        assert result.containment_scope == "cli_client"
        assert result.quiescent is False
        assert result.containment["survivors"] == []
        # 轮询而不是立刻判定：fork 后到被调度起来之间有一个小窗口。
        alive = False
        for _ in range(40):
            try:
                os.kill(delegatee, 0)
                alive = True
                break
            except ProcessLookupError:
                time.sleep(0.05)
        if not alive:
            # 前提建立不起来：这个沙箱会回收已脱离会话的后台子进程（GitHub runner
            # 实测如此），那就无法证明「扫描没看见它」——因为根本不存在可被扫描的
            # 存活进程。按仓库既有原则：环境缺口记 SKIP，不算通过，也不伪装成红。
            pytest.skip("sandbox reaps detached children; containment premise unavailable")
    finally:
        with suppress(ProcessLookupError):
            os.kill(delegatee, signal.SIGKILL)


def test_session_creator_without_a_host_store_factory_refuses_to_guess():
    from agentcore.colleague_hermes_runner import HermesGatewaySessionCreator

    creator = HermesGatewaySessionCreator(
        connection_for_profile=lambda profile: object(),
        cwd_for_profile=lambda profile: "/tmp")
    with pytest.raises(RuntimeError, match="session_store_factory_required"):
        creator.create("reviewer_profile", "req-1")
