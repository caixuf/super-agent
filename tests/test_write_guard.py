"""write-guard 的两条纪律必须同时钉住三件事：该拒的拒、该报的报、**正常的绝不动**。

误伤一个正常短写入比漏掉一个超长写入更糟：前者会让每次写文件都多一条假报错，
模型会去重写已经正确的文件，人也开始不信这个钩子。所以「不误伤」是验收判据之一，
不是附带检查。

三条判据（对应修复指令的验收口径）：
  ① 正例：content > 阈值 → 被拒 + 提示分段；
  ② 反例：写入落空 → 回读校验报错；
  ③ 不误伤：正常短写入 → 行为不变（pre 放行、post 不报错、结果原文不变）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("write_guard", ROOT / "plugins" / "write_guard" / "__init__.py")
assert _spec and _spec.loader
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# ── 夹具：清掉可能影响判定的环境变量，避免用例间串味 ────────────────────
@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("WRITE_GUARD_OFF", raising=False)
    monkeypatch.delenv("WRITE_GUARD_MAX_CHARS", raising=False)
    yield


def _ok_result(path: str, content: str) -> str:
    """工具成功返回的真实形状（含 verified / resolved_path / bytes_written）。"""
    return json.dumps({
        "bytes_written": len(content.encode("utf-8")),
        "dirs_created": True,
        "verified": True,
        "resolved_path": path,
        "files_modified": [path],
    }, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
# ① 正例：超长 content 被拒 + 提示分段
# ══════════════════════════════════════════════════════════════════════

def test_oversized_content_is_blocked():
    content = "x" * (guard.DEFAULT_MAX_CHARS + 1)
    out = guard._on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/a.md", "content": content})
    assert out is not None, "超长 content 必须被拒，不能静默放过去"
    assert out["action"] == "block"


def test_block_message_teaches_chunking():
    """拒绝语必须**可自纠**：说清原因 + 怎么改，否则模型只会原地重试同样的长内容。"""
    content = "y" * (guard.DEFAULT_MAX_CHARS + 100)
    out = guard._on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/a.md", "content": content})
    msg = out["message"]
    assert str(len(content)) in msg, "要说出实际长度，模型才知道要砍多少"
    assert "8000" in msg, "要说出上限"
    assert "分块" in msg or "append" in msg, "必须给出分段写法的具体建议"


def test_threshold_boundary_is_inclusive():
    """恰好等于阈值不该被拒 —— 阈值语义是「超过才拒」，边界含糊会导致每次都在临界点抖动。"""
    at_limit = "z" * guard.DEFAULT_MAX_CHARS
    assert guard._on_pre_tool_call(tool_name="write_file",
                                   args={"path": "/tmp/a.md", "content": at_limit}) is None


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("WRITE_GUARD_MAX_CHARS", "100")
    assert guard.max_chars() == 100
    assert guard._on_pre_tool_call(tool_name="write_file",
                                   args={"path": "/tmp/a.md", "content": "q" * 101}) is not None


def test_bad_threshold_env_falls_back_to_default(monkeypatch):
    """配错阈值不能变成「不限」——那是把唯一的护栏拆掉。"""
    monkeypatch.setenv("WRITE_GUARD_MAX_CHARS", "not-a-number")
    assert guard.max_chars() == guard.DEFAULT_MAX_CHARS
    monkeypatch.setenv("WRITE_GUARD_MAX_CHARS", "-5")
    assert guard.max_chars() == guard.DEFAULT_MAX_CHARS


# ══════════════════════════════════════════════════════════════════════
# ② 反例：写入落空 → 回读校验报错
# ══════════════════════════════════════════════════════════════════════

def test_readback_flags_missing_file(tmp_path):
    """工具说成功，但文件不存在 —— 这就是「整篇落空」的核心场景。"""
    missing = str(tmp_path / "never_written.md")
    verdict = guard.readback_verdict(missing, 100)
    assert verdict, "文件不存在必须报错"
    assert "不存在" in verdict


def test_readback_flags_truncated_file(tmp_path):
    """磁盘字节数明显少于期望 → 内容被截断（写少了）。"""
    target = tmp_path / "short.md"
    target.write_text("abc", encoding="utf-8")
    assert guard.readback_verdict(str(target), 5000), "字节数远少于期望必须报错"


def test_readback_flags_non_regular_file(tmp_path):
    target = tmp_path / "a_dir"
    target.mkdir()
    assert guard.readback_verdict(str(target), 10), "目标是目录时必须报错"


def test_failed_write_is_surfaced_into_result(tmp_path):
    """端到端（单进程）：写落空 → 结果里出现 error。

    这是「失败写入被当场报错」的真实链路，也是本插件唯一能让模型看见错误的通道
    —— 因为 post_tool_call 的返回值被框架忽略。

    注意：**不先调 post**。真实 agent 路径上 transform 跑在 post 之前，
    所以结论必须由 transform 自己回读得出，不能靠 post 先算好再传过来。
    """
    missing = str(tmp_path / "gone.md")
    content = "hello world"
    args = {"path": missing, "content": content}
    result = _ok_result(missing, content)  # 工具「以为」写成功了
    out = guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                          task_id="t1", session_id="s1", tool_call_id="tc1")
    assert out is not None, "回读失败必须被注回结果，否则模型永远不知道自己没写成"
    parsed = json.loads(out)
    assert parsed.get("error"), "必须置 error，让展示层与模型都按错误处理"
    assert "不存在" in parsed["error"]


def test_transform_is_order_independent(tmp_path):
    """transform **先于** post 触发（实测：transform 在 handle_function_call 内，post 在其后）。

    所以结论不能靠 post 先算好再传 —— transform 必须自己回读。
    """
    missing = str(tmp_path / "gone.md")
    args = {"path": missing, "content": "hello"}
    result = _ok_result(missing, "hello")
    # 只调 transform（post 还没发生）
    out = guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                          task_id="", session_id="", tool_call_id="x")
    assert out is not None and json.loads(out).get("error")


def test_post_still_checks_when_tool_already_errored(tmp_path):
    """工具报错时**仍然回读**：「文件没落盘」这个事实必须由回读确认。

    验收判据要求「失败写入被当场报错」—— 只信工具自己的错误是不够的，
    工具说失败不代表文件状态已知。结论以结构化事实附上，不重复制造错误文本。
    """
    missing = str(tmp_path / "gone.md")
    args = {"path": missing, "content": "x"}
    err_result = json.dumps({"error": "permission denied"})
    guard._on_post_tool_call(tool_name="write_file", args=args, result=err_result,
                             task_id="", session_id="", tool_call_id="call-2")
    out = guard._on_transform_tool_result(tool_name="write_file", args=args, result=err_result,
                                          task_id="", session_id="", tool_call_id="call-2")
    assert out is not None, "工具报错后仍要回读并给出结论"
    parsed = json.loads(out)
    assert parsed["readback"]["ok"] is False
    assert "不存在" in parsed["readback"]["detail"]
    # 工具自己的错误原因不被改写（不制造第二条错误文本）
    assert parsed["error"] == "permission denied"


def test_readback_uses_resolved_path_from_result(tmp_path):
    """相对路径必须靠结果里的 resolved_path 回读，不能拿原始相对路径去 Path()。

    这是最阴的一处误伤：write_file 把相对路径锚定到 task 的 base dir，
    与本进程 cwd 不同，直接用原始相对路径会**每次都判文件不存在**。
    """
    real = tmp_path / "real.md"
    real.write_text("hello", encoding="utf-8")
    args = {"path": "real.md", "content": "hello"}          # 相对路径
    result = _ok_result(str(real), "hello")                  # 工具回报绝对路径
    out = guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                          task_id="", session_id="", tool_call_id="call-3")
    assert out is not None
    parsed = json.loads(out)
    assert parsed["readback"]["ok"] is True, "靠 resolved_path 回读必须判为通过"
    assert "error" not in parsed


def test_relative_path_without_resolved_path_is_not_judged(monkeypatch):
    """拿不到可信绝对路径时**不判失败**：判据不足不等于失败。"""
    args = {"path": "some/relative.md", "content": "x"}
    result = json.dumps({"bytes_written": 1})   # 没有 resolved_path / files_modified
    assert guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                           task_id="", session_id="", tool_call_id="call-4") is None


# ══════════════════════════════════════════════════════════════════════
# ③ 不误伤：正常短写入行为不变
# ══════════════════════════════════════════════════════════════════════

def test_normal_short_write_passes_pre_hook():
    out = guard._on_pre_tool_call(tool_name="write_file",
                                  args={"path": "/tmp/a.md", "content": "# 短内容\n几行而已。"})
    assert out is None, "正常短写入必须放行"


def test_normal_short_write_result_is_untouched(tmp_path):
    """正常写入：回读通过，结果**不含任何 error**，且带 readback.ok=True。

    「行为不变」的口径：工具的真实结论不被改写、不被追加错误；只多一个
    校验通过的标记（这就是「验过写了」的意思）。
    """
    target = tmp_path / "ok.md"
    content = "# 标题\n\n正文若干。\n"
    target.write_text(content, encoding="utf-8")
    args = {"path": str(target), "content": content}
    result = _ok_result(str(target), content)
    out = guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                          task_id="", session_id="", tool_call_id="call-5")
    assert out is not None, "回读通过也要留下「验过」的痕迹"
    parsed = json.loads(out)
    assert parsed.get("readback", {}).get("ok") is True
    assert "error" not in parsed, "正常写入不得被改成错误"
    assert parsed["bytes_written"] == len(content.encode("utf-8")), "原有字段必须原样保留"


def test_other_tools_are_never_touched():
    """别的工具即便参数里带超长 content 也不该被拦（patch 有自己的 diff 语义）。"""
    huge = "x" * 20000
    assert guard._on_pre_tool_call(tool_name="patch",
                                   args={"path": "/tmp/a.md", "new_string": huge}) is None
    assert guard._on_pre_tool_call(tool_name="terminal",
                                   args={"command": f"echo {huge[:50]}"}) is None


def test_malformed_args_are_safe():
    """pre_tool_call 是 fail-closed：任何畸形输入都必须安全返回、不能抛。

    注意 ``{}`` 是**故意拦下**的（截断签名），不是「安全放行」——
    真正该放行的是「参数根本不是 dict」与「content 类型错」（工具自己会报更准的诊断）。
    """
    assert guard._on_pre_tool_call(tool_name="write_file", args=None) is None
    assert guard._on_pre_tool_call(tool_name="write_file", args=[]) is None
    assert guard._on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/a", "content": 123}) is None
    assert guard._on_pre_tool_call(tool_name="write_file", args={"content": "x" * 9000}) is not None


# ══════════════════════════════════════════════════════════════════════
# ①bis 兜底：参数被输出上限截断后剩下的空壳
# ══════════════════════════════════════════════════════════════════════

def test_missing_content_argument_is_blocked():
    """截断发生在**钩子之前**：工具层把残缺参数换成空对象，钩子只看到 ``{}``。

    这时正文已经彻底丢了 —— 如果不拦，agent 会把这次调用当成「写成功了」，
    于是「以为写了」正是长内容落空的机制。必须 block 并说清内容已丢失。
    """
    out = guard._on_pre_tool_call(tool_name="write_file", args={"path": "/tmp/a.md"})
    assert out is not None and out["action"] == "block"
    assert "content" in out["message"]
    assert "lost" in out["message"].lower() or "丢" in out["message"]


def test_empty_args_are_blocked_as_truncated():
    """``{}`` 是截断的典型签名（Unrepairable → replaced with empty object）。"""
    out = guard._on_pre_tool_call(tool_name="write_file", args={})
    assert out is not None and out["action"] == "block"


def test_empty_content_string_is_allowed():
    """content 为空串是合法诉求（清空文件），不能当成截断误伤。"""
    assert guard._on_pre_tool_call(tool_name="write_file",
                                   args={"path": "/tmp/a.md", "content": ""}) is None


def test_off_switch_disables_both_checks(monkeypatch, tmp_path):
    """一键止血：关掉之后超长也放行、回读失败也不报。"""
    monkeypatch.setenv("WRITE_GUARD_OFF", "1")
    assert guard._on_pre_tool_call(tool_name="write_file",
                                   args={"path": "/tmp/a.md", "content": "x" * 20000}) is None
    missing = str(tmp_path / "gone.md")
    args = {"path": missing, "content": "x"}
    result = _ok_result(missing, "x")
    assert guard._on_transform_tool_result(tool_name="write_file", args=args, result=result,
                                           task_id="", session_id="", tool_call_id="call-6") is None


def test_original_args_are_not_mutated():
    original = {"path": "/tmp/a.md", "content": "x" * 9000}
    guard._on_pre_tool_call(tool_name="write_file", args=original)
    assert set(original) == {"path", "content"}, "插件不得就地改调用方的 dict"


def test_post_hook_logs_but_never_raises(tmp_path):
    """post 钩子只留日志，不抛错、不改结果（observer hook 的返回值被框架忽略）。"""
    missing = str(tmp_path / "gone.md")
    args = {"path": missing, "content": "x"}
    result = json.dumps({"error": "permission denied"})
    assert guard._on_post_tool_call(tool_name="write_file", args=args, result=result,
                                    task_id="", session_id="", tool_call_id="c") is None
    # 畸形输入也不抛
    assert guard._on_post_tool_call(tool_name="write_file", args=None, result=None) is None
    assert guard._on_post_tool_call(tool_name="terminal", args={"command": "ls"},
                                    task_id="", session_id="", tool_call_id="c") is None
