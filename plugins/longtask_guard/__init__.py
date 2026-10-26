"""longtask-guard —— 把「长任务」在工具派发前自动转成后台执行。

为什么需要它（这是纪律的代码化，不是偏好）
-------------------------------------------
长任务前台跑会占住整个回合：用户几十秒到几分钟收不到任何消息，回合还会在轮数上限处被截断，
最后连"任务已提交"这句话都发不出去。这件事此前只写在提示词/记忆里（"T2 必须后台化"），
结果就是**偶尔忘、偶尔懒** —— 靠纪律约束的行为一定会漏。

本插件用 `pre_tool_call` 的 `modify` 指令，在 `terminal` 真正执行前改写参数：

* 命中长任务特征 → 自动置 `background=true` + `notify_on_complete=true`（完成时通知，不守着轮询）；
* 前台 `timeout` 超过阈值 → 同样转后台（前台上限本来也等不到）。

判定必须"宁窄勿宽"：误拦一个 3 秒的 `git status` 比漏掉一个长任务更烦人。
所以只认**明确的耗时动作**，并提供 `LONGTASK_GUARD_OFF=1` 一键关闭。

它不改工具语义，也不隐藏任何东西：命令照跑，只是不再霸占当前回合。
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Any, Optional

logger = logging.getLogger(__name__)

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
    ("sleep ", "显式长等待"),
)

#: 前台超时超过它就一定等不到（工具侧前台上限 600s），直接转后台。
_FOREGROUND_TIMEOUT_CAP_S = 600


def _is_off() -> bool:
    return os.environ.get("LONGTASK_GUARD_OFF", "").strip() not in ("", "0", "false", "False")


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

#: 解释器的"只看不跑"开关：`bash -n` 语法检查、`python -c` 一行、`python -m` 模块。
_NOEXEC_FLAGS: frozenset[str] = frozenset({"-n", "-c", "--version", "-V", "-h", "--help"})


def _match_segment(segment: str) -> Optional[str]:
    """在一个命令片段里找长任务特征。只看**命令本体位置**，不看参数与字符串。

    判据分三步，每一步都在挡一类误伤：
      1. 首 token 是只读包裹器（grep/echo/git/...）→ 它后面全是参数，整段不判；
      2. 首 token 是解释器（bash/python/...）→ 看它后面的脚本名，但 `-n`/`-c` 这类
         "只看不跑"的开关直接放行（`bash -n verify.sh` 是语法检查，几毫秒）；
      3. 否则按可执行名/脚本路径精确匹配，**不做子串包含**。
    """
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

    for pattern, label in _HEAVY_PATTERNS:
        pat = pattern.strip()
        if " " in pat:
            if " ".join(tokens[:3]).startswith(pat) or pat in " ".join(tokens[:3]):
                return f"{label}（{pat}）"
            continue
        if exe == pat or tokens[0].endswith("/" + pat):
            return f"{label}（{pat}）"
    return None


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

    try:
        timeout = int(args.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0
    if timeout > _FOREGROUND_TIMEOUT_CAP_S:
        return f"前台超时 {timeout}s 超过上限"

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
    patched["notify_on_complete"] = True
    # 后台跑就别再传前台 timeout（它的语义是"等这么久"，后台没有等待者）。
    patched.pop("timeout", None)
    logger.info("longtask-guard: terminal 转后台（%s）", reason)
    return {"action": "modify", "args": patched}


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
