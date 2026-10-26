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


def test_already_background_is_left_alone():
    """已经是后台就别再改 —— 重复改写会让 notify 语义漂移。"""
    assert _hit("pytest tests", background=True) is None


# ── 负例：这些要么很快、要么只是"提到"长任务的名字，绝不能动 ─────────────
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
