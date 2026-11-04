"""卡片原地更新重试补丁的行为测试。

不连真实消息平台，只钉住三件事：

1. **可恢复故障会重试**：第一次 DNS 失败、第二次成功 → 调用方拿到成功结果，
   卡片形态保住（不会降级）。
2. **不可恢复故障立刻失败**：4xx 语义错误不重试，一次就抛（重试只会拖长降级）。
3. **重试耗尽才降级**：一直失败时，异常照旧抛给调用方，原有降级路径不受影响。

补丁本身是往宿主安装树写代码，所以测试直接驱动打补丁后的模块级函数；打补丁
的动作由 ``patch_feishu_update_retry.py --apply`` 完成，这里不重复实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCHER = REPO_ROOT / "patches" / "patch_feishu_update_retry.py"
SOURCE_ADAPTER = Path.home() / ".hermes" / "hermes-agent" / "plugins" / "platforms" / "feishu" / "adapter.py"


def _patched_module(tmp_path: Path):
    """把打补丁后的辅助函数装进一个最小模块，只测重试语义。"""
    if not SOURCE_ADAPTER.is_file():
        pytest.skip("宿主 adapter 不在本机，跳过")
    target = tmp_path / "plugins" / "platforms" / "feishu"
    target.mkdir(parents=True)
    shutil.copy2(SOURCE_ADAPTER, target / "adapter.py")
    result = subprocess.run(
        [sys.executable, str(PATCHER), "--apply", "--agent-dir", str(tmp_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip("补丁无法应用到本机树：%s" % (result.stdout + result.stderr)[:200])
    text = (target / "adapter.py").read_text(encoding="utf-8")
    start = text.index("#: 卡片/文本原地更新失败后是否值得重试。")
    end = text.index("# --- Regex patterns ---")
    body = text[start:end]
    namespace: dict = {"asyncio": asyncio, "logger": _QuietLogger()}
    exec(compile(body, "<patched-helpers>", "exec"), namespace)  # noqa: S102 — 被测代码就来自这里
    return namespace


class _QuietLogger:
    def __init__(self):
        self.messages: list[str] = []

    def warning(self, fmt, *args):
        self.messages.append(fmt % args)


class _Boom(Exception):
    """非 requests 异常的兜底分支：按可恢复处理。"""


def test_retryable_covers_network_and_5xx(tmp_path):
    ns = _patched_module(tmp_path)
    retryable = ns["_feishu_update_retryable"]

    assert retryable(ConnectionError("Temporary failure in name resolution")) is True
    assert retryable(TimeoutError("timed out")) is True

    requests = pytest.importorskip("requests", reason="宿主未装 requests")
    assert retryable(requests.ConnectionError("dns")) is True
    assert retryable(requests.Timeout("read")) is True

    for status, expected in ((503, True), (502, True), (429, True), (408, True), (500, True), (504, True)):
        exc = requests.HTTPError("http")
        exc.response = type("R", (), {"status_code": status})()
        assert retryable(exc) is expected, status

    for status in (400, 401, 403, 404):
        exc = requests.HTTPError("http")
        exc.response = type("R", (), {"status_code": status})()
        assert retryable(exc) is False, status


def test_transient_failure_is_retried_then_succeeds(tmp_path):
    """第一次失败、第二次成功 —— 这正是 DNS 抖动该有的结果。"""
    ns = _patched_module(tmp_path)
    with_retry = ns["_feishu_update_with_retry"]
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Temporary failure in name resolution")
        return "ok"

    result = asyncio.run(with_retry(flaky, what="Card patch"))
    assert result == "ok"
    assert calls["n"] == 2, "一次可恢复失败必须触发一次重试"


def test_semantic_error_fails_once_without_retry(tmp_path):
    """4xx 语义错误不是抖动，重试只会拖长降级。"""
    ns = _patched_module(tmp_path)
    with_retry = ns["_feishu_update_with_retry"]
    requests = pytest.importorskip("requests", reason="宿主未装 requests")
    calls = {"n": 0}

    async def rejected():
        calls["n"] += 1
        exc = requests.HTTPError("bad request")
        exc.response = type("R", (), {"status_code": 400})()
        raise exc

    with pytest.raises(requests.HTTPError):
        asyncio.run(with_retry(rejected, what="Card patch"))
    assert calls["n"] == 1, "4xx 不得重试"


def test_exhausted_retries_raise_for_caller_fallback(tmp_path):
    """一直失败 → 抛给调用方，原有降级路径保持不变。"""
    ns = _patched_module(tmp_path)
    with_retry = ns["_feishu_update_with_retry"]
    calls = {"n": 0}

    async def always_down():
        calls["n"] += 1
        raise ConnectionError("network down")

    with pytest.raises(ConnectionError):
        asyncio.run(with_retry(always_down, what="Card patch", attempts=3))
    assert calls["n"] == 3, "重试次数应与发送路径一致"


def test_patch_is_idempotent(tmp_path):
    """重复 apply 不得重复插入。"""
    if not SOURCE_ADAPTER.is_file():
        pytest.skip("宿主 adapter 不在本机，跳过")
    target = tmp_path / "plugins" / "platforms" / "feishu"
    target.mkdir(parents=True)
    shutil.copy2(SOURCE_ADAPTER, target / "adapter.py")
    first = subprocess.run([sys.executable, str(PATCHER), "--apply", "--agent-dir", str(tmp_path)],
                           capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    once = (target / "adapter.py").read_text(encoding="utf-8")
    second = subprocess.run([sys.executable, str(PATCHER), "--apply", "--agent-dir", str(tmp_path)],
                            capture_output=True, text=True)
    assert second.returncode == 0, second.stdout + second.stderr
    twice = (target / "adapter.py").read_text(encoding="utf-8")
    assert once == twice, "第二次 apply 不得改动文件"
    assert twice.count("def _feishu_update_retryable") == 1


def _load_patcher():
    spec = importlib.util.spec_from_file_location("_update_retry_patcher", PATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drift_is_reported_and_refused(tmp_path):
    """上游改了锚点形态时，补丁必须报出是哪一处并拒绝写入（退出码 2）。

    静默跳过是最坏的结局：安装器以为成功，用户继续看到降级的回复。
    """
    if not SOURCE_ADAPTER.is_file():
        pytest.skip("宿主 adapter 不在本机，跳过")
    patcher = _load_patcher()
    target = tmp_path / "plugins" / "platforms" / "feishu"
    target.mkdir(parents=True)
    # 从**未打过**的原版出发：宿主树上那份可能已经打过了，probe 在位会掩盖漂移。
    text = _pristine_adapter_text(patcher)
    # 上游把 patch 调用改名 = card-frame 那一处形态真的变了
    drifted_text = text.replace("self._client.im.v1.message.patch",
                                "self._client.im.v1.message.patch_renamed")
    assert drifted_text != text, "构造漂移失败：锚点没命中，本用例失去意义"
    (target / "adapter.py").write_text(drifted_text, encoding="utf-8")

    todos, drifted = patcher._missing(drifted_text)
    assert any("card-frame" in d for d in drifted), drifted
    assert not any(name == "card-frame" for name, _o, _n in todos)

    result = subprocess.run([sys.executable, str(PATCHER), "--apply", "--agent-dir", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 2, result.stdout
    assert "card-frame" in result.stdout
    assert (target / "adapter.py").read_text(encoding="utf-8") == drifted_text, "拒绝写入时不得改动文件"


def _pristine_adapter_text(patcher) -> str:
    """宿主 adapter 的**未打补丁**原文。

    宿主树上那份可能已经打过了，直接拿它做漂移实验会被在位探针掩盖 —— 探针在，
    每一步都判「已到位」，漂移永远看不见。这里用补丁自带的 old/new 反向还原。
    """
    text = SOURCE_ADAPTER.read_text(encoding="utf-8")
    for _name, old, new, _what, _probe in patcher.STEPS:
        text = text.replace(new, old)
    return text
