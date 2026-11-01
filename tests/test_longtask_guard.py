"""longtask-guard 的判定必须同时钉住两件事：**该转的转**，**不该动的绝不动**。

误伤一个 0.2 秒的查询比漏掉一个长任务更糟：前者让每次调用都多一次后台往返，
用户会立刻感到"它怎么什么都不当场回答"。所以负例数量刻意多于正例。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("longtask_guard", ROOT / "plugins" / "longtask_guard" / "__init__.py")
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _hit(command: str, **extra) -> str | None:
    return guard.classify({"command": command, **extra})


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """断言\"默认行为\"的用例不能被宿主 profile 的 .env 污染。

    插件开关是给网关进程用的（``LONGTASK_GUARD_*`` 落在 profile 的 .env 里），
    但 ``pytest`` 继承同一个环境 —— 不隔离的话，机器上装了开关就会把
    \"默认值\"的用例判红，红得还很像回归。每个用例自己决定要不要设。
    """
    for name in ("LONGTASK_GUARD_OFF", "LONGTASK_GUARD_NOTIFY", "LONGTASK_GUARD_HEARTBEAT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(guard, "_CTX", None)
    yield


# ── 正例：这些确实会跑很久，必须转后台 ──────────────────────────────
@pytest.mark.parametrize("command", [
    "python3 -m pytest tests -q",
    "/usr/bin/python3 -m pytest tests/unit -v",
    "docker build -t x .",
    "bash docker/verify.sh 6333c92",
    "bash verify.sh --with-docker",
    "./run.sh build",
    "git clone https://example.com/a/b.git",
    "pip install -r requirements.txt",
])
def test_heavy_commands_are_converted(command):
    assert _hit(command), f"应转后台却漏了：{command}"


@pytest.mark.parametrize("command", [
    "cd /repo && bash docker/verify.sh",     # && 连接，第二段是长任务
    "echo start; sleep 120; echo done",       # 复合命令里有长等待
])
def test_compound_commands_are_detected(command):
    assert _hit(command), f"复合命令应命中：{command}"


def test_timeout_env_wrapped_pytest_converts_even_with_tool_timeout():
    assert _hit(
        "timeout 550 env PATH=/usr/bin:$PATH python3 -m pytest tests/unit -q",
        timeout=580,
    )


def test_short_declared_timeout_keeps_the_call_in_the_foreground():
    """A caller who names a wait budget wants the result in this very call.

    The guard exists to stop a *hidden* long wait from occupying the turn. When
    the caller already bounded the wait, converting it to a background spawn
    hides the very exit code they asked for -- the terminal returns "started"
    and the verification they were about to do reads as unverified.
    """
    assert _hit("pytest tests/auth -q", timeout=120) is None
    assert _hit("docker build -t img .", timeout=90) is None


def test_a_long_declared_timeout_still_converts():
    """A generous budget is a safety net, not a request to block this call."""
    assert _hit("pytest tests -q", timeout=580)


def test_the_threshold_is_configurable(monkeypatch):
    """The ceiling comes from the environment at call time, not at import time."""
    monkeypatch.setenv("LONGTASK_GUARD_FOREGROUND_TIMEOUT", "300")
    assert guard.classify({"command": "pytest tests -q", "timeout": 240}) is None
    assert guard.classify({"command": "pytest tests -q", "timeout": 400})

    monkeypatch.setenv("LONGTASK_GUARD_FOREGROUND_TIMEOUT", "0")
    assert guard.classify({"command": "pytest tests -q", "timeout": 60})


def test_a_malformed_threshold_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LONGTASK_GUARD_FOREGROUND_TIMEOUT", "not-a-number")
    assert guard.classify({"command": "pytest tests -q", "timeout": 120}) is None


def test_already_background_is_left_alone():
    """已经是后台就别再改 —— 重复改写会让 notify 语义漂移。"""
    assert _hit("pytest tests", background=True) is None


# ── sleep：短等待不转后台（转后台 = 多一次完成唤醒，净亏）────────────────
@pytest.mark.parametrize("command", [
    "sleep 5",
    "sleep 20",
    "sleep 29",
    "sleep 0.5",
    "sleep 20; ls -la",
])
def test_short_sleeps_are_never_converted(command):
    """20 秒的等待转后台，省下的 20 秒不值得多开一个回合。"""
    assert _hit(command) is None, f"短 sleep 不应转后台：{command}"


@pytest.mark.parametrize("command", [
    "sleep 30",
    "sleep 120",
    "echo start; sleep 120; echo done",
    "sleep 2m",
    "sleep 1h",
])
def test_long_sleeps_are_converted(command):
    assert _hit(command), f"长等待应转后台：{command}"


def test_sleep_with_unparsable_duration_is_left_alone():
    """判不了时长就不动 —— 转错比漏转更烦人。"""
    assert _hit("sleep $DELAY") is None


# ── 唤醒策略归 profile，不归插件 ──────────────────────────────────────
class _CtxNotify:
    """只提供 get_config 的最小插件上下文。"""

    def __init__(self, settings: dict):
        self._settings = settings
        self.profile_name = "test"

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_hook(self, *_a, **_k):
        return None


@pytest.mark.parametrize("command", [
    "git status --short",
    "ls -la",
    "cat README.md",
    "grep -rn pytest scripts/",                 # 只是搜这个词
    "echo 'docker build'",                       # 只是在字符串里提到
    "grep -c 'run.sh build' docs/x.md",
    "git log --oneline -5",
    "curl -s localhost:9900/api/autotest/health",
    "python3 -c 'print(1)'",
    "bash -n verify.sh",                         # 只做语法检查，不执行
])
def test_fast_commands_are_never_touched(command):
    assert _hit(command) is None, f"误伤快速命令：{command}"


def test_non_terminal_calls_are_ignored():
    """别的工具即便参数里带长任务字样也不该被改写。"""
    out = guard._on_pre_tool_call(tool_name="write_file", args={"path": "a.py", "content": "pytest"})
    assert out is None


def test_empty_and_malformed_args_are_safe():
    """pre_tool_call 抛错会拦工具，所以任何畸形输入都必须安全返回 None。"""
    assert guard._on_pre_tool_call(tool_name="terminal", args=None) is None
    assert guard._on_pre_tool_call(tool_name="terminal", args={}) is None
    assert guard._on_pre_tool_call(tool_name="terminal", args={"command": ""}) is None
    assert guard._on_pre_tool_call(tool_name="terminal", args={"command": 123}) is None


def test_off_switch_disables_conversion(monkeypatch):
    monkeypatch.setenv("LONGTASK_GUARD_OFF", "1")
    assert guard._on_pre_tool_call(tool_name="terminal",
                                   args={"command": "pytest tests"}) is None


# ── 改写本身的行为 ────────────────────────────────────────────────
def test_modify_directive_sets_background_and_notify():
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert out and out["action"] == "modify"
    assert out["args"]["background"] is True
    assert out["args"]["notify_on_complete"] is True


def test_modify_drops_foreground_timeout():
    """后台没有等待者，留着前台 timeout 只会让调用方误读语义。"""
    out = guard._on_pre_tool_call(tool_name="terminal",
                                  args={"command": "pytest tests", "timeout": 900})
    assert out and "timeout" not in out["args"]


def test_original_args_are_not_mutated():
    """插件绝不能就地改调用方传进来的 dict —— 那是共享对象。"""
    original = {"command": "pytest tests -q"}
    guard._on_pre_tool_call(tool_name="terminal", args=original)
    assert "background" not in original


@pytest.mark.parametrize("command", [
    "timeout 550 env PATH=/usr/bin:$PATH /usr/bin/python3 -m pytest tests/unit -q",
    "timeout -k 5s 550s env -u PYTHONPATH /usr/bin/python3 -m pytest tests -q",
    "env A=1 B=2 timeout 120 python3 -m pytest tests -q",
    "bash -lc 'timeout 550 env PATH=/usr/bin:$PATH python3 -m pytest tests/unit'",
])
def test_execution_wrappers_do_not_hide_suites(command):
    assert _hit(command)


@pytest.mark.parametrize("command", [
    "sed -n '1,80p' /var/log/agent.log",
    "awk 'NR >= 1 && NR <= 80 { print }' /var/log/agent.log",
    "for f in /var/log/agent.log; do printf '%s\\n' \"$f\"; done",
])
def test_timeout_120_does_not_background_short_read_queries(command):
    assert _hit(command, timeout=120) is None


def test_timeout_alone_does_not_background_unknown_command():
    assert _hit("custom-runner --check", timeout=580) is None


# ── 执行期心跳与活体账本 ──────────────────────────────────────────
def test_conversion_arms_heartbeat(monkeypatch):
    monkeypatch.delenv("LONGTASK_GUARD_HEARTBEAT", raising=False)
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert out and out["args"]["heartbeat"] == 120


def test_heartbeat_can_be_disabled_or_clamped(monkeypatch):
    monkeypatch.setenv("LONGTASK_GUARD_HEARTBEAT", "0")
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert "heartbeat" not in out["args"]
    monkeypatch.setenv("LONGTASK_GUARD_HEARTBEAT", "5")
    assert guard._heartbeat_seconds() == 60
    monkeypatch.setenv("LONGTASK_GUARD_HEARTBEAT", "garbage")
    assert guard._heartbeat_seconds() == 120


class _FakeLedger:
    def __init__(self):
        self.register_calls = []
        self.exit_calls = []

    def register(self, *args, **kwargs):
        self.register_calls.append((args, kwargs))

    def record_exit(self, *args, **kwargs):
        self.exit_calls.append((args, kwargs))


class _Ctx:
    profile_name = "p"


def test_post_hook_registers_background_process(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    guard._on_post_tool_call(
        tool_name="terminal", args={"command": "pytest tests", "background": True},
        result={"session_id": "proc_1", "pid": 123, "status": "yielded_to_background", "exit_code": 0},
        session_id="s1")
    assert len(fake.register_calls) == 1
    args, kwargs = fake.register_calls[0]
    assert args == ("proc_1",)
    assert kwargs["kind"] == "terminal"
    assert kwargs["owner"] == "p:s1"
    assert kwargs["pid"] == 123
    assert kwargs["state"] == "running"
    assert fake.exit_calls == []  # background receipts carry exit_code=0 while alive


def test_post_hook_records_exit_only_for_completion(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    guard._on_post_tool_call(
        tool_name="terminal",
        args={"command": "pytest tests", "background": True},
        result={"session_id": "proc_2", "status": "exited", "exit_code": 0, "output": "ok"},
        session_id="s1")
    assert fake.register_calls and fake.exit_calls
    assert fake.exit_calls[0][0] == ("proc_2", 0)


def test_post_hook_ignores_foreground_and_never_raises(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    guard._on_post_tool_call(tool_name="terminal", args={"command": "ls"},
                             result={"session_id": "proc_1"}, session_id="s1")
    assert fake.register_calls == []

    def boom():
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(guard, "_ledger", boom)
    assert guard._on_post_tool_call(
        tool_name="terminal", args={"command": "pytest", "background": True},
        result={"session_id": "proc_1"}, session_id="s1") is None


def test_post_hook_writes_a_real_ledger_row(monkeypatch, tmp_path):
    from agentcore.liveness import LivenessLedger

    class Ctx:
        profile_name = "p"

        def get_config(self, key, default=None):
            if key == "liveness_db":
                return str(tmp_path / "liveness.db")
            if key == "engine_root":
                return str(ROOT)
            return default

    guard._CACHE.clear()
    monkeypatch.setattr(guard, "_CTX", Ctx())
    guard._on_post_tool_call(
        tool_name="terminal", args={"command": "pytest tests", "background": True},
        result={"session_id": "proc_1", "pid": None, "status": "yielded_to_background"},
        session_id="s1")
    store = LivenessLedger(tmp_path / "liveness.db")
    row = store.get("proc_1", require_owner="p:s1")
    assert row is not None
    assert row["state"] == "running"
    # `label` is what the progress card shows, so it is a readable name -- not
    # the shell line. The command itself is kept in `detail` for debugging.
    assert row["label"] == "跑测试套（pytest）"
    assert row["detail"] == "pytest tests"


def test_register_wires_both_hooks():
    class Ctx:
        profile_name = "p"

        def __init__(self):
            self.hooks = {}

        def get_config(self, key, default=None):
            return default

        def register_hook(self, name, fn):
            self.hooks[name] = fn

    ctx = Ctx()
    guard.register(ctx)
    assert set(ctx.hooks) == {"pre_tool_call", "post_tool_call"}
    assert ctx.hooks["post_tool_call"] is guard._on_post_tool_call


# ── 回执形态识别：宿主可能把「改写前」的参数交给 post 钩子 ────────────────
# 现场实证：远端宿主 上 27 次转后台、账本一条没落 —— post 钩子拿到的是模型原始参数
# （没有 background），于是 old 代码直接 return。回执本身能证明这是后台进程。
def test_post_hook_registers_from_a_spawn_receipt_without_background_args(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    guard._on_post_tool_call(
        tool_name="terminal",
        args={"command": "pip install -q markdown", "timeout": 300},   # 原始参数：没有 background
        result={"output": "Background process started", "session_id": "proc_7a6d7683664e",
                "pid": 7342, "exit_code": 0, "error": None, "notify_on_complete": True},
        session_id="session-one")
    assert len(fake.register_calls) == 1
    args, kwargs = fake.register_calls[0]
    assert args == ("proc_7a6d7683664e",)
    assert kwargs["pid"] == 7342 and kwargs["state"] == "running"
    assert fake.exit_calls == []          # spawn receipt carries exit_code=0 while alive


def test_post_hook_registers_from_a_yield_receipt(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    guard._on_post_tool_call(
        tool_name="terminal",
        args={"command": "pytest tests", "timeout": 300},
        result={"output": "collected 12 items", "returncode": None,
                "yielded_session_id": "proc_ac42e2e34dbb", "pid": 371772,
                "notify_on_complete": True},
        session_id="session-one")
    assert [call[0] for call in fake.register_calls] == [("proc_ac42e2e34dbb",)]


def test_post_hook_ignores_a_foreground_result_quoting_the_spawn_note(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    # stdout that happens to equal the spawn note, but no tracked process: never a spawn.
    guard._on_post_tool_call(tool_name="terminal", args={"command": "echo ..."},
                             result={"output": "Background process started", "exit_code": 0},
                             session_id="s1")
    # a tail that merely mentions the note while a process id is present is not a spawn either.
    guard._on_post_tool_call(tool_name="terminal", args={"command": "pytest tests"},
                             result={"output": "Background process started\nok", "session_id": "proc_1"},
                             session_id="s1")
    assert fake.register_calls == []

# ── 唤醒策略：profile 说不要，就别每次完成都开新回合 ──────────────────
def test_notify_defaults_to_on(monkeypatch):
    monkeypatch.setattr(guard, "_CTX", None)
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert out and out["args"]["notify_on_complete"] is True


def test_profile_can_request_silent_background(monkeypatch):
    monkeypatch.setattr(guard, "_CTX", _CtxNotify({"notify_on_complete": False}))
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert out and out["action"] == "modify"
    assert out["args"]["background"] is True          # 仍然转后台
    assert out["args"]["notify_on_complete"] is False # 但不唤醒
    assert "heartbeat" not in out["args"]             # 心跳也是唤醒源，一并关掉


def test_env_switch_disables_notification(monkeypatch):
    monkeypatch.setattr(guard, "_CTX", None)
    monkeypatch.setenv("LONGTASK_GUARD_NOTIFY", "0")
    out = guard._on_pre_tool_call(tool_name="terminal", args={"command": "pytest tests -q"})
    assert out and out["args"]["notify_on_complete"] is False


def test_silent_background_still_reaches_the_ledger(monkeypatch):
    """关掉唤醒不等于放弃可观测性：账本照记，task_status 仍能查到。"""
    fake = _FakeLedger()
    monkeypatch.setattr(guard, "_ledger", lambda: fake)
    monkeypatch.setattr(guard, "_CTX", _Ctx())
    guard._on_post_tool_call(
        tool_name="terminal",
        args={"command": "pytest tests -q"},
        result={"output": "Background process started", "session_id": "proc_beef1234",
                "pid": 42, "exit_code": 0, "error": None, "notify_on_complete": False},
        session_id="session-one")
    assert len(fake.register_calls) == 1


# --------------------------------------------------------------------------- #
# Progress-card naming: a shell line is not a label
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command,expected", [
    # A wrapped, piped, redirected command still resolves to the work it does.
    ('cd /srv/app && /usr/bin/python3 -m pytest bridge/tests -q 2>&1 | tail -5',
     "跑测试套（pytest）"),
    ("docker build -t img .", "建镜像（docker build）"),
    # Not a known long task: fall back to the program name plus its arguments.
    ("npm run build", "npm run build"),
    ("git status", "git status"),
    # A leading `cd` names no work on its own; the next segment decides.
    ("cd /srv/app && make release", "make release"),
    # Flags are dropped, positional arguments are kept: they name the target.
    ("ls -la /tmp", "ls /tmp"),
])
def test_readable_label_names_the_work_not_the_shell_line(command, expected):
    assert guard.readable_label(command) == expected


def test_readable_label_is_bounded_and_never_empty_for_empty_input():
    assert guard.readable_label("") == ""
    assert guard.readable_label("   ") == ""
    long_cmd = "cd /x && " + "runner " + " ".join("a%d" % i for i in range(40))
    assert 0 < len(guard.readable_label(long_cmd)) <= 60
