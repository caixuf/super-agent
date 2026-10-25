"""Pi 执行器核心：进程拉起 / JSON 事件流解析 / 回执汇总。

为什么不能只看 exit code
------------------------
Pi 官方 ``docs/cli-integration.md`` 明确：

    * JSON 模式下，**失败或被中止的 assistant 回复本身不会让退出码非零**，
      必须检查事件流里的 ``message_end``（stop reason ``error`` / ``aborted`` /
      ``length``）；
    * ``length``（撞到输出 token 上限被截断）只打印部分文本 + 警告到 stderr；
    * ``agent_end`` 之后可能还有自动重试 / 压缩恢复 / 排队工作，
      **``agent_settled`` 才代表本次运行没有后续自动动作**。

所以本模块以事件流为权威：``exit 0`` 只作为必要不充分条件之一。
（Pi 1.4.0（本机安装版）确实发 ``agent_settled``；对更老的版本只有
``agent_end`` 的情况，走 ``agent_end`` 回退并在回执 ``warnings`` 里显式标注版本适配。）

事件形状（实测 Pi 1.4.0）::

    {"type":"session","version":3,"id":..,"cwd":..}
    {"type":"agent_start"}
    {"type":"turn_start"} / {"type":"turn_end","message":..,"toolResults":..}
    {"type":"message_start","message":{..}}
    {"type":"message_update","usage":..,"assistantMessageEvent":{..}}   # 增量
    {"type":"message_end","message":{..}}                              # 权威终值
    {"type":"tool_execution_start","toolCallId":..,"toolName":..,"args":{..}}
    {"type":"tool_execution_end","toolCallId":..,"toolName":..,"result":..,"isError":..,"durationMs":..}
    {"type":"agent_end","messages":[..],"willRetry":false}
    {"type":"agent_settled","aborted":false}
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 模型别名的唯一事实源在 ccp_config：改默认模型只改那一处。
from .ccp_config import DEFAULT_CCP_ALIAS  # noqa: E402

_logger = logging.getLogger("hermes_bridge.pi_executor")

# 会被 Pi 当作「失败 / 中止 / 截断」的 assistant stop reason。
FAILURE_STOP_REASONS = ("error", "aborted", "length")


class PiRunError(RuntimeError):
    """执行器自身的前置条件不满足（node/cli 缺失、参数非法等）。

    注意：这不是「Pi 跑出来的失败」。Pi 跑出来失败（stop reason error 等）
    会正常返回 ``PiResult``，由 ``ok=False`` 表达，而不是抛异常。
    """


class PiTimeoutError(PiRunError):
    """超时。保留为独立类型方便调用方区分「超时」与「配置错误」。"""


@dataclass
class PiResult:
    """一次 Pi 调用的结构化回执。

    ``ok`` 的语义是 **「这次调用没有以错误 / 中止 / 截断收场」**，
    不是「测试通过」「业务成功」。调用方拿最终输出与产物自行验收。
    """

    ok: bool = False
    exit_code: int | None = None
    settled: bool = False
    settled_aborted: bool = False
    stop_reason: str | None = None
    model: str | None = None
    provider: str | None = None
    final_text: str = ""
    tool_activity: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    usage: dict[str, Any] | None = None
    truncated: bool = False
    error_message: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = 0
    session_id: str | None = None
    events_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # 语义常量，防止下游把 ok 误读成「测试通过」。
    success_semantics: str = "invocation_completed_without_error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "success_semantics": self.success_semantics,
            "exit_code": self.exit_code,
            "settled": self.settled,
            "settled_aborted": self.settled_aborted,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "provider": self.provider,
            "final_text": self.final_text,
            "tool_activity": self.tool_activity,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "truncated": self.truncated,
            "error_message": self.error_message,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "events_count": self.events_count,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# 环境发现
# --------------------------------------------------------------------------- #

def _default_install_dir() -> Path:
    """隔离安装目录默认位置（可用 ``PI_EXECUTOR_INSTALL_DIR`` 覆盖）。"""
    env = os.environ.get("PI_EXECUTOR_INSTALL_DIR")
    if env:
        return Path(env).expanduser()
    profile = os.environ.get("HERMES_PROFILE", "default")
    return Path(f"~/.hermes/profiles/{profile}/cache/scratch/pi_isolated").expanduser()


def resolve_cli_path(cli_path: str | os.PathLike | None = None) -> str:
    """定位可执行入口：显式参数 > 环境变量 > PATH 上的 ``pi`` > 隔离安装目录。

    返回可直接交给 ``node`` 的脚本路径（``dist/bundle/cli.js``），
    或 PATH 上的 ``pi`` 可执行文件。
    """
    if cli_path:
        p = Path(cli_path).expanduser()
        if not p.exists():
            raise PiRunError(f"指定的 Pi CLI 不存在: {p}")
        return str(p)

    for env_name in ("PI_EXECUTOR_CLI", "PI_CLI_BIN"):
        env = os.environ.get(env_name)
        if env:
            p = Path(env).expanduser()
            if not p.exists():
                raise PiRunError(f"{env_name} 指向的 Pi CLI 不存在: {p}")
            return str(p)

    on_path = shutil.which("pi")
    if on_path:
        return on_path

    bundle = _default_install_dir() / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    if bundle.exists():
        return str(bundle)
    raise PiRunError(
        "找不到 Pi CLI。请设置 PI_EXECUTOR_CLI，或在隔离目录安装："
        f"cd {_default_install_dir().parent} && npm install --ignore-scripts @earendil-works/pi-coding-agent"
    )


def resolve_node(node_bin: str | None = None, *, min_major: int = 22, min_minor: int = 19) -> str:
    """定位 ``node`` 并校验引擎下限（package engines: ``>=22.19.0``）。"""
    node = node_bin or os.environ.get("PI_NODE_BIN") or shutil.which("node")
    if not node:
        raise PiRunError("找不到 node（Pi 的 engines 要求 node >=22.19.0）")
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - 环境相关
        raise PiRunError(f"无法执行 node: {exc}") from exc
    ver = out.stdout.strip().lstrip("v")
    try:
        major, minor = (int(x) for x in ver.split(".")[:2])
    except ValueError as exc:
        raise PiRunError(f"无法解析 node 版本: {out.stdout!r}") from exc
    if (major, minor) < (min_major, min_minor):
        raise PiRunError(f"node {ver} 低于 Pi 引擎下限 {min_major}.{min_minor}.0")
    return node


# --------------------------------------------------------------------------- #
# 参数构造（数组，不 shell 拼接）
# --------------------------------------------------------------------------- #

def build_argv(
    prompt: str,
    *,
    cli_path: str,
    model: str,
    provider: str,
    tools: str | None = "read,write",
    no_session: bool = True,
) -> list[str]:
    """构造 Pi 的参数数组。

    显式使用数组 —— 调用方（``subprocess``）不做 shell 解析，
    prompt 里的引号 / 空格 / 分号不会被当命令执行。
    ``--`` 终止选项解析，避免 prompt 以 ``-`` 开头时被当成选项。
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise PiRunError("prompt 不能为空")
    argv = [cli_path, "--mode", "json"]
    if no_session:
        argv.append("--no-session")
    argv += ["--provider", provider, "--model", model]
    if tools:
        argv += ["--tools", tools]
    argv += ["--", prompt]
    return argv


# --------------------------------------------------------------------------- #
# JSONL 解析
# --------------------------------------------------------------------------- #

def parse_jsonl(text: str) -> list[dict[str, Any]]:
    """严格按 LF 切分 JSONL（官方：只在 LF 上切，忽略 CR；不按 Unicode 分隔符切）。

    容忍最后一行无换行；无法解析的行**跳过**（不整体失败），
    但返回的事件数会与行数解耦，便于上层判断是否有脏行。
    """
    events: list[dict[str, Any]] = []
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            # 逐行容错：脏行跳过而不让整次调用失败，但留下痕迹便于排障。
            _logger.debug("跳过无法解析的 JSONL 行: %s", exc)
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _extract_text(content: Any) -> str:
    """从 assistant message 的 content 里取纯文本（忽略 thinking / toolcall 块）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    return ""


# --------------------------------------------------------------------------- #
# 事件流 -> 回执
# --------------------------------------------------------------------------- #

def summarize_events(
    events: Iterable[dict[str, Any]],
    *,
    exit_code: int | None,
    timed_out: bool = False,
    cancelled: bool = False,
    duration_ms: int = 0,
    stderr_tail: str = "",
) -> PiResult:
    """把事件流汇总成回执。**纯函数**：不读文件、不拉进程，便于单测造事件。"""
    settled = False
    settled_aborted = False
    saw_agent_end = False
    agent_end_will_retry = False
    stop_reason: str | None = None
    model: str | None = None
    provider: str | None = None
    usage: dict[str, Any] | None = None
    final_text = ""
    session_id: str | None = None
    tool_stats: dict[str, dict[str, Any]] = {}
    tool_pending: dict[str, str] = {}
    count = 0

    for ev in events:
        count += 1
        etype = ev.get("type")
        if etype == "session":
            sid = ev.get("id")
            session_id = sid if isinstance(sid, str) else session_id
        elif etype == "agent_end":
            saw_agent_end = True
            agent_end_will_retry = bool(ev.get("willRetry"))
        elif etype == "agent_settled":
            settled = True
            settled_aborted = bool(ev.get("aborted"))
        elif etype == "message_end":
            msg = ev.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                sr = msg.get("stopReason")
                if isinstance(sr, str):
                    stop_reason = sr
                mv = msg.get("model")
                if isinstance(mv, str):
                    model = mv
                pv = msg.get("provider")
                if isinstance(pv, str):
                    provider = pv
                uv = msg.get("usage")
                if isinstance(uv, dict):
                    usage = uv
                text = _extract_text(msg.get("content"))
                if text:
                    final_text = text
        elif etype == "tool_execution_start":
            tcid = ev.get("toolCallId")
            name = ev.get("toolName")
            if isinstance(tcid, str) and isinstance(name, str):
                tool_pending[tcid] = name
        elif etype == "tool_execution_end":
            name = ev.get("toolName")
            if not isinstance(name, str):
                name = tool_pending.get(ev.get("toolCallId", ""), "unknown")
            stat = tool_stats.setdefault(name, {"tool": name, "calls": 0, "errors": 0, "total_ms": 0})
            stat["calls"] += 1
            if ev.get("isError"):
                stat["errors"] += 1
            dur = ev.get("durationMs")
            if isinstance(dur, (int, float)):
                stat["total_ms"] += int(dur)

    warnings: list[str] = []
    if not settled:
        if saw_agent_end:
            # 版本适配：老版本只发 agent_end。willRetry=true 视为未稳定。
            warnings.append("agent_settled_missing_fallback_agent_end")
            settled = not agent_end_will_retry
        else:
            warnings.append("no_agent_settled_event")

    error_message: str | None = None
    if stop_reason == "error":
        error_message = "assistant stop reason=error"
    elif stop_reason == "aborted":
        error_message = "assistant stop reason=aborted"
    elif stop_reason == "length":
        error_message = "assistant stop reason=length (输出被 token 上限截断)"
    elif timed_out:
        error_message = "执行超时，已清理进程树"
    elif cancelled:
        error_message = "调用被取消，已清理进程树"
    elif exit_code not in (0, None):
        error_message = f"退出码非零: {exit_code}"
    elif not settled:
        error_message = "未观察到 agent_settled（运行未稳定结束）"

    truncated = stop_reason == "length"

    ok = (
        exit_code == 0
        and settled
        and not settled_aborted
        and not timed_out
        and not cancelled
        and stop_reason not in FAILURE_STOP_REASONS
    )

    return PiResult(
        ok=ok,
        exit_code=exit_code,
        settled=settled,
        settled_aborted=settled_aborted,
        stop_reason=stop_reason,
        model=model,
        provider=provider,
        final_text=final_text,
        tool_activity=list(tool_stats.values()),
        tool_calls=sum(s["calls"] for s in tool_stats.values()),
        usage=usage,
        truncated=truncated,
        error_message=error_message,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=duration_ms,
        session_id=session_id,
        events_count=count,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# 进程拉起
# --------------------------------------------------------------------------- #

def _kill_tree(proc: subprocess.Popen, *, grace_s: float = 5.0) -> None:
    """清理整棵进程树（含 node 子进程）。先 SIGTERM，宽限后 SIGKILL。

    用 ``start_new_session=True`` 让子进程自成进程组，``killpg`` 才能一次带走全部。
    """
    if proc.poll() is not None:
        return
    # posix 下用进程组
    try:
        pgid = os.getpgid(proc.pid)
    except OSError as exc:
        _logger.debug("getpgid(%s) 失败，退回单进程信号: %s", proc.pid, exc)
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:  # pragma: no cover - 非 posix 兜底
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError) as exc:
            _logger.debug("信号 %s 目标已消失/无权，视为已清理: %s", sig, exc)
            break
        try:
            proc.wait(timeout=grace_s)
            break
        except subprocess.TimeoutExpired as exc:
            _logger.debug("宽限期后仍在运行，升级到下一个信号: %s", exc)
            continue
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover
        _logger.warning("SIGKILL 后 wait 仍超时，交给 OS 回收 pid=%s", proc.pid)


def run_pi(
    prompt: str,
    *,
    workdir: str | os.PathLike,
    agent_dir: str | os.PathLike,
    model: str = DEFAULT_CCP_ALIAS,
    provider: str = "ccp",
    tools: str | None = "read,write",
    timeout_s: float = 300.0,
    cli_path: str | os.PathLike | None = None,
    node_bin: str | None = None,
    no_session: bool = True,
    env_extra: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    events_path: str | os.PathLike | None = None,
    command_override: list[str] | None = None,
) -> PiResult:
    """在隔离目录里跑一次 Pi，返回结构化回执。

    ``workdir`` 与 ``agent_dir`` 都必须显式给出（不默认到生产仓 / 全局 ``~/.pi``）。
    ``command_override`` 仅供测试注入假进程（mock 事件回归用），
    生产调用不要传。
    """
    workdir_p = Path(workdir).expanduser().resolve()
    agent_dir_p = Path(agent_dir).expanduser().resolve()
    if not workdir_p.is_dir():
        raise PiRunError(f"workdir 不存在: {workdir_p}")

    if command_override is not None:
        # 仅供测试：完整替换 argv，不再解析 node / cli。
        argv = list(command_override)
    else:
        cli = resolve_cli_path(cli_path)
        node = resolve_node(node_bin)
        argv = [node, *build_argv(
            prompt, cli_path=cli, model=model, provider=provider,
            tools=tools, no_session=no_session,
        )]

    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir_p)
    env["PI_OFFLINE"] = "1"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["PI_TELEMETRY"] = "0"
    # 本地 CCP 不走代理
    for key in ("NO_PROXY", "no_proxy"):
        cur = env.get(key, "")
        for host in ("127.0.0.1", "localhost"):
            if host not in cur:
                cur = f"{cur},{host}" if cur else host
        env[key] = cur
    if env_extra:
        env.update(env_extra)

    t0 = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=str(workdir_p),
        env=env,
        # Pi 在 print / RPC 模式下会等到 stdin 的 EOF 才开始跑。子进程若继承父
        # 进程的 stdin（父进程是常驻服务，从不关闭），请求永远发不出去、也没有
        # 任何输出——表现是"静默挂死到超时"，而不是报错。所以必须显式给
        # /dev/null，让 Pi 立刻看到 EOF。
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )

    captured: dict[str, str] = {}

    def _reader(stream, key: str) -> None:  # type: ignore[no-untyped-def]
        try:
            captured[key] = stream.read() or ""
        except Exception as exc:  # pragma: no cover
            _logger.debug("读取子进程 %s 流失败（不影响回执）: %s", key, exc)
            captured[key] = ""
        finally:
            try:
                stream.close()
            except Exception as exc:  # pragma: no cover
                _logger.debug("关闭子进程 %s 流失败: %s", key, exc)

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for t in threads:
        t.start()

    timed_out = False
    cancelled = False
    deadline = t0 + float(timeout_s)
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _kill_tree(proc)
            break
        if time.monotonic() > deadline:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(0.05)

    exit_code = proc.returncode
    for t in threads:
        t.join(timeout=5.0)
    duration_ms = int((time.monotonic() - t0) * 1000)

    raw_stdout = captured.get("stdout", "")
    raw_stderr = captured.get("stderr", "")

    if events_path is not None:
        # 原始事件流含 prompt 与工具参数 —— 仅在显式要求时落盘，且由调用方放到隔离目录。
        try:
            Path(events_path).expanduser().write_text(raw_stdout, encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            _logger.warning("事件流落盘失败（不影响回执）: %s", exc)

    events = parse_jsonl(raw_stdout)
    stderr_tail = raw_stderr[-2000:]
    return summarize_events(
        events,
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=duration_ms,
        stderr_tail=stderr_tail,
    )