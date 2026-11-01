"""longtask-guard —— 把「长任务」在工具派发前自动转成后台执行。

为什么需要它（这是纪律的代码化，不是偏好）
-------------------------------------------
长任务前台跑会占住整个回合：用户几十秒到几分钟收不到任何消息，回合还会在轮数上限处被截断，
最后连"任务已提交"这句话都发不出去。这件事此前只写在提示词/记忆里（"T2 必须后台化"），
结果就是**偶尔忘、偶尔懒** —— 靠纪律约束的行为一定会漏。

本插件用 `pre_tool_call` 的 `modify` 指令，在 `terminal` 真正执行前改写参数：

* 命中长任务特征 → 自动置 `background=true` + `notify_on_complete=true`（完成时通知，不守着轮询）；
* 同时带 `heartbeat`（默认 120s，`LONGTASK_GUARD_HEARTBEAT=0` 可关），并在 `post_tool_call`
  把返回的 `session_id/pid` 登记进 `agentcore.liveness` 账本，供 `task_status` 查询；
* timeout/env 等包裹器会递归剥离，按实际执行的命令判断；超时数值本身不判重。

判定必须"宁窄勿宽"：误拦一个 3 秒的 `git status` 比漏掉一个长任务更烦人。
所以只认**明确的耗时动作**，并提供 `LONGTASK_GUARD_OFF=1` 一键关闭。

它不改工具语义，也不隐藏任何东西：命令照跑，只是不再霸占当前回合。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Set by ``register``; used only to find the profile and the engine checkout.
_CTX = None
_CACHE: dict = {}
_DEFAULT_HEARTBEAT_SECONDS = 120


def _heartbeat_seconds() -> int:
    """Mid-run heartbeat interval for converted jobs; 0 disables it.

    The host clamps to its own floor (60s) and skips quiet ticks, so this is a
    cadence, not a promise that every tick produces a message.
    """
    raw = os.environ.get("LONGTASK_GUARD_HEARTBEAT", str(_DEFAULT_HEARTBEAT_SECONDS))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = _DEFAULT_HEARTBEAT_SECONDS
    return max(60, value) if value > 0 else 0


def _load_liveness(engine_root: Optional[str] = None):
    try:
        from agentcore import liveness  # type: ignore
        return liveness
    except ModuleNotFoundError as error:
        if error.name not in ("agentcore", "agentcore.liveness"):
            raise
    if not isinstance(engine_root, str) or not Path(engine_root).is_absolute():
        raise ValueError("engine_root is required when agentcore is not importable")
    path = Path(engine_root).resolve(strict=True) / "agentcore" / "liveness.py"
    key = ("module", str(path))
    if key in _CACHE:
        return _CACHE[key]
    spec = importlib.util.spec_from_file_location("agentcore_liveness", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the liveness ledger")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agentcore_liveness"] = module
    spec.loader.exec_module(module)
    _CACHE[key] = module
    return module


def _ledger():
    """Ledger for this profile, or ``None`` when the engine cannot be resolved."""
    if _CTX is None:
        return None
    try:
        module = _load_liveness(_CTX.get_config("engine_root"))
        path = _CTX.get_config("liveness_db") or None
        key = ("ledger", str(path))
        if key not in _CACHE:
            _CACHE[key] = module.LivenessLedger(path)
        return _CACHE[key]
    except Exception:  # noqa: BLE001 — ledger absence must never block the tool
        logger.debug("longtask-guard: liveness ledger unavailable", exc_info=True)
        return None


#: The host's own background-spawn receipt (``terminal(background=True)`` result body).
_SPAWN_RECEIPT_OUTPUT = "Background process started"
#: A declared foreground wait budget at or below this many seconds is honoured:
#: the caller asked for a result, not for a spawn receipt. 180s keeps an ordinary
#: test run in the foreground while still catching a genuinely long command.
DEFAULT_FOREGROUND_TIMEOUT = 180.0
#: Tracked background process ids: ``proc_`` + short hex suffix.
_PROCESS_ID = re.compile(r"proc_[0-9a-f]{1,}")


def _named_process(payload: dict) -> Optional[str]:
    """The tracked background process a terminal result names, if it names one.

    The spawn result carries ``session_id``; the yield-to-background result carries
    ``yielded_session_id``. Anything else is not a background identity.
    """
    for key in ("session_id", "yielded_session_id"):
        value = payload.get(key)
        if isinstance(value, str) and _PROCESS_ID.fullmatch(value):
            return value
    return None


def _parse_result(result: Any) -> Optional[dict]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            value = json.loads(result)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                       session_id: str = "", **_: Any) -> None:
    """Record a background process's identity; never raise into hook dispatch."""
    if tool_name != "terminal" or not isinstance(args, dict):
        return None
    payload = _parse_result(result)
    if payload is None:
        return None
    process_id = _named_process(payload)
    background = (bool(args.get("background"))
                  or payload.get("status") in ("running", "yielded_to_background")
                  # The host may hand this hook the model's *original* arguments rather than
                  # the rewritten ones, so the receipt itself is the other proof: a spawn result
                  # carries the spawn note, a yield result carries ``yielded_session_id``.
                  or (process_id is not None
                      and (payload.get("output") == _SPAWN_RECEIPT_OUTPUT
                           or payload.get("yielded_session_id") == process_id)))
    if not background or process_id is None:
        return None
    try:
        ledger = _ledger()
        logger.debug("longtask-guard post: path=%r session=%r",
                     getattr(ledger, "path", None), session_id)
        if ledger is None:
            return None
        profile = getattr(_CTX, "profile_name", "") or "local"
        owner = "%s:%s" % (profile, session_id or "local")
        command = str(payload.get("command") or args.get("command") or "")
        # The card shows `label`; a truncated shell line there reads as noise.
        # Keep the readable name in `label` and the command itself in `detail`,
        # where the single-task view can still show it for debugging.
        label = readable_label(command)
        pid = payload.get("pid")
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        ledger.register(process_id, kind="terminal", owner=owner, label=label,
                        pid=pid, host="local", phase="started", state="running",
                        detail=command.strip()[:120])
        # A background start receipt carries exit_code=0 even while the child is
        # still running. Only a completion-shaped status may close the row.
        status = payload.get("status")
        code = payload.get("exit_code")
        if code is not None and status not in (None, "running", "yielded_to_background"):
            ledger.record_exit(process_id, code, tail=payload.get("output") or "")
    except Exception:  # noqa: BLE001 — a ledger write must not fail the tool result
        logger.warning("longtask-guard: ledger registration failed", exc_info=True)
    return None

#: 前台等不到、或等到了也把回合占死的动作特征（按 shell 片段匹配）。
#: 每条都对应一类实测会跑很久的命令；**故意不写宽泛的 `python`/`make`**——
#: 那些既可能是 0.2 秒的探针，也可能是 10 分钟的构建，误伤成本高于收益。
_HEAVY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("pytest", "跑测试套"),
    ("docker build", "建镜像"),
    ("docker/verify.sh", "容器内端到端验证"),
    ("verify.sh", "自检脚本"),
    ("run.sh build", "编译并跑基线"),
    ("git clone", "克隆仓库"),
    ("pip install", "装依赖"),
    ("npm install", "装依赖"),
    ("apt-get install", "装依赖"),
)

#: ``sleep`` 只有够久才值得转后台。``sleep 20`` 这类短等待转成后台，
#: 代价是**多一次进程完成唤醒**，换来的只是省 20 秒 —— 净亏。
_SLEEP_BACKGROUND_MIN_SECONDS = 30
_SLEEP_UNITS: dict[str, int] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _sleep_seconds(args: list[str]) -> Optional[float]:
    """``sleep`` 的时长（秒）；参数无法解析时 ``None``（判不了就不动它）。"""
    total = 0.0
    for raw in args:
        match = re.fullmatch(r"([0-9]*\.?[0-9]+)([smhd]?)", str(raw).strip())
        if not match:
            return None
        total += float(match.group(1)) * _SLEEP_UNITS[match.group(2)]
    return total


def _is_off() -> bool:
    return os.environ.get("LONGTASK_GUARD_OFF", "").strip() not in ("", "0", "false", "False")


def _notify_on_complete() -> bool:
    """转后台后是否唤醒 agent 开一个新回合。

    默认 True（保持历史行为）；profile 可在插件设置里写 ``notify_on_complete: false``
    来只要后台、不要唤醒 —— 宿主的 ``display.background_process_notifications`` 管不住
    ``notify_on_complete``，所以这个策略只能在这里表达。
    环境变量 ``LONGTASK_GUARD_NOTIFY=0`` 同样可关。
    """
    if os.environ.get("LONGTASK_GUARD_NOTIFY", "").strip() in ("0", "false", "False", "no"):
        return False
    if _CTX is None:
        return True
    try:
        return _CTX.get_config("notify_on_complete", True) is not False
    except Exception:  # noqa: BLE001 — 配置读不到就按默认（唤醒）走
        return True


def _shell_segments(command: str) -> list[str]:
    """命令按 `;` `&&` `||` 切成片段，供逐段匹配（避免整串误命中）。"""
    out: list[str] = []
    for chunk in command.replace("||", ";").replace("&&", ";").split(";"):
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


#: 只读/不执行的包裹器：它们后面跟的脚本/名字**不会真的跑起来**。
#: 少了这条，`grep pytest`、`bash -n verify.sh` 这类会被误判成长任务。
_READONLY_WRAPPERS: frozenset[str] = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "echo", "printf", "cat", "head", "tail", "less", "more",
    "ls", "find", "which", "type", "stat", "file",
    "curl", "wget", "test", "[",
})

#: git 不能一刀切：`git status` 毫秒级，`git clone/fetch/pull/push` 可能几分钟。
#: 按子命令分流，别用"整体豁免"图省事 —— 那会把 clone 漏成前台长任务。
_GIT_HEAVY_SUBCOMMANDS: frozenset[str] = frozenset({"clone", "fetch", "pull", "push", "submodule", "gc"})

#: 解释器：真正执行的是它**后面**那一段，所以要往下看。
_INTERPRETERS: frozenset[str] = frozenset({"bash", "sh", "zsh", "python", "python3", "node", "npx", "sudo", "env"})

#: 解释器的"只看不跑"开关：`bash -n` 语法检查、`python --version` 等；shell -c 实际执行，不能当作只读。
_NOEXEC_FLAGS: frozenset[str] = frozenset({"-n", "-c", "--version", "-V", "-h", "--help"})


def _match_segment(segment: str, _depth: int = 0) -> Optional[str]:
    """在一个命令片段里找长任务特征。只看**命令本体位置**，不看参数与字符串。

    判据分三步，每一步都在挡一类误伤：
      1. 首 token 是只读包裹器（grep/echo/git/...）→ 它后面全是参数，整段不判；
      2. 首 token 是解释器（bash/python/...）→ 看它后面的脚本名，但 `bash -n` 这类
         只做语法检查的开关直接放行（`bash -n verify.sh` 是语法检查，几毫秒）；
      3. 否则按可执行名/脚本路径精确匹配，**不做子串包含**。
    """
    if _depth > 8:
        return None
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None

    # 环境变量前缀（FOO=1 pytest ...）先跳过，否则首 token 是赋值语句
    while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
        tokens = tokens[1:]
    if not tokens:
        return None

    exe = tokens[0].rsplit("/", 1)[-1]
    # 包裹器不是真正执行的程序；逐层解析，不把pytest藏在timeout/env后面。
    if exe == "timeout":
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option == "--":
                break
            if option in ("-k", "--kill-after", "-s", "--signal"):
                if not rest:
                    return None
                rest.pop(0)
        if len(rest) < 2:
            return None
        return _match_segment(shlex.join(rest[1:]), _depth + 1)
    if exe in ("env", "sudo"):
        rest = tokens[1:]
        while rest:
            word = rest[0]
            if word == "--":
                rest.pop(0)
                break
            if word in ("-u", "--unset", "-C", "--chdir", "--user", "-g", "--group"):
                rest = rest[2:]
            elif word.startswith("-") or (exe == "env" and "=" in word):
                rest.pop(0)
            else:
                break
        return _match_segment(shlex.join(rest), _depth + 1) if rest else None
    if exe in ("bash", "sh", "zsh") and len(tokens) > 2 and tokens[1] in ("-c", "-lc"):
        return next((hit for part in _shell_segments(tokens[2])
                     if (hit := _match_segment(part, _depth + 1))), None)
    if exe == "git":
        # 按子命令分流：只读子命令放行，clone/fetch/pull/push 这类照转后台。
        sub = next((t for t in tokens[1:] if not t.startswith("-")), "")
        return "拉/推代码（git 网络子命令）" if sub in _GIT_HEAVY_SUBCOMMANDS else None
    if exe in _READONLY_WRAPPERS:
        return None

    if exe in _INTERPRETERS:
        rest = tokens[1:]
        if any(t in _NOEXEC_FLAGS for t in rest[:2]):
            return None
        # `python -m pytest`：模块名本身就是长任务
        if len(rest) >= 2 and rest[0] == "-m":
            rest = [rest[1]] + rest[2:]
        tokens = rest or []
        if not tokens:
            return None
        exe = tokens[0].rsplit("/", 1)[-1]

    if exe == "sleep":
        # 只有够久的等待才值得转后台；短 sleep 转后台是净亏（多一次完成唤醒）。
        seconds = _sleep_seconds(tokens[1:])
        if seconds is None or seconds < _SLEEP_BACKGROUND_MIN_SECONDS:
            return None
        return f"显式长等待（{seconds:g}s）"

    for pattern, label in _HEAVY_PATTERNS:
        pat = pattern.strip()
        if " " in pat:
            if " ".join(tokens[:3]).startswith(pat) or pat in " ".join(tokens[:3]):
                return f"{label}（{pat}）"
            continue
        if exe == pat or tokens[0].endswith("/" + pat):
            return f"{label}（{pat}）"
    return None


#: Shell builtins and noise words that name no work when they open a segment.
_SHELL_NOISE = frozenset({"cd", "echo", "export", "set", "true", "time", "env", "nohup"})


def readable_label(command: str) -> str:
    """A short human name for a shell command, for the progress card.

    The card is read by a person waiting on the task; a truncated shell line
    tells them nothing. Reuse the long-task classifier's vocabulary
    ("跑测试套（pytest）") and otherwise fall back to the program name plus its
    leading arguments. Pure, so the naming can be pinned by a test.
    """
    text = (command or "").strip()
    if not text:
        return ""
    segments = _shell_segments(text)
    for segment in segments:
        hit = _match_segment(segment)
        if hit:
            return hit
    for segment in segments:
        parts = segment.split()
        if not parts or parts[0].rsplit("/", 1)[-1] in _SHELL_NOISE:
            continue
        args = [t for t in parts[1:] if not t.startswith("-")][:2]
        return " ".join([parts[0].rsplit("/", 1)[-1], *args])[:60]
    return text.rsplit("/", 1)[-1][:60]


def _foreground_timeout_ceiling() -> float:
    """A declared wait budget at or below this many seconds stays foreground.

    Rationale: the guard exists to stop a *hidden* long wait from occupying the
    turn. Once the caller writes an explicit ``timeout``, the wait is not hidden
    — and turning the call into a background spawn throws away the exit code
    they asked for. Zero disables the exemption.
    """
    raw = os.environ.get("LONGTASK_GUARD_FOREGROUND_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_FOREGROUND_TIMEOUT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FOREGROUND_TIMEOUT


def classify(args: dict[str, Any]) -> Optional[str]:
    """返回该 `terminal` 调用命中的长任务说明；不是长任务则 None。纯函数，便于钉住判定。"""
    if not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    # 已经是后台就没必要动它（重复改写会让 notify 语义漂移）。
    if args.get("background"):
        return None

    # 调用方已经写死了等待上限，说明他要的就是这次调用的结果（通常是退出码）。
    # 把它改成后台，等于把答案换成了句“已启动”。
    ceiling = _foreground_timeout_ceiling()
    declared = args.get("timeout")
    if ceiling > 0 and isinstance(declared, (int, float)) and 0 < declared <= ceiling:
        return None

    for segment in _shell_segments(command):
        hit = _match_segment(segment)
        if hit:
            return hit
    return None


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict[str, Any]]:
    """把命中长任务特征的 `terminal` 改写成后台调用。永不抛错（pre_tool_call 失败会拦工具）。"""
    if tool_name != "terminal" or _is_off() or not isinstance(args, dict):
        return None
    try:
        reason = classify(args)
    except Exception:  # 判定出问题就放行，绝不因为插件故障卡住工具
        logger.debug("longtask-guard classify failed", exc_info=True)
        return None
    if not reason:
        return None
    patched = dict(args)
    patched["background"] = True
    # 是否唤醒 agent 是 profile 的策略，不是插件的策略：本插件只负责"别占住回合"。
    # 关掉后仍然转后台、仍然进活体账本（task_status 可查），只是不再每次完成都开一个新回合 ——
    # "同一件事被反复回一遍"就是这么来的。
    notify = _notify_on_complete()
    patched["notify_on_complete"] = notify
    heartbeat = _heartbeat_seconds() if notify else 0
    if heartbeat:
        patched["heartbeat"] = heartbeat
    else:
        patched.pop("heartbeat", None)
    # 后台跑就别再传前台 timeout（它的语义是"等这么久"，后台没有等待者）。
    patched.pop("timeout", None)
    logger.info("longtask-guard: terminal 转后台（%s）", reason)
    return {"action": "modify", "args": patched}


def register(ctx) -> None:
    global _CTX
    logger.debug("longtask-guard register: profile=%r engine_root=%r liveness_db=%r",
                 getattr(ctx, 'profile_name', None), ctx.get_config('engine_root'),
                 ctx.get_config('liveness_db'))
    _CTX = ctx
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
