"""Validate and render one automatic task from its five-part contract.

    python3 tools/autotask/taskfile.py validate jobs/*.json
    python3 tools/autotask/taskfile.py render jobs/memory-budget.json

A task file is data, not code: ``collect`` / ``write`` / ``report`` / ``retry`` are executed by
the host scheduler's prompt and checked here; ``judge`` is the only free text the model reads.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
import sys

ALLOWED_WRITE_ROOTS = ("state/", "skills/candidates/")
CRON = re.compile(r"^[\d*/,\- ]+$")
REQUIRED = ("id", "schedule", "collect", "judge", "write", "report")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_path_problems(path, profile=""):
    """Writes stay under the autotask state dir or the candidates dir; no traversal, no absolutes."""
    if not path:
        return ["write.path is empty"]
    if ".." in Path(path).parts:
        return ["write.path must be free of '..'"]
    if path.startswith("/") and profile:
        resolved = Path(path).resolve()
        if Path(profile).resolve() not in resolved.parents:
            return ["write.path must stay inside the profile (%s)" % profile]
    tail = path.replace("<profile>/", "", 1)
    if any(tail.startswith(root) for root in ALLOWED_WRITE_ROOTS):
        return []
    return ["write.path must start with <profile>/%s, got %s" % (" or ".join(ALLOWED_WRITE_ROOTS), path)]


def validate(task):
    """Return a list of problems; empty means the task can be rendered."""
    problems = []
    for key in REQUIRED:
        if key not in task:
            problems.append("missing %s" % key)
    if problems:
        return problems
    if not CRON.match(task["schedule"]) or len(task["schedule"].split()) != 5:
        problems.append("schedule must be a 5-field cron expression")
    collect = task["collect"]
    if not isinstance(collect, dict) or collect.get("kind") not in {"files", "command", "none"}:
        problems.append("collect.kind must be files | command | none")
    elif collect["kind"] == "files" and not collect.get("paths"):
        problems.append("collect.paths required for kind=files")
    elif collect["kind"] == "command" and not collect.get("command"):
        problems.append("collect.command required for kind=command")
    write = task["write"]
    if not isinstance(write, dict) or write.get("kind") != "file":
        problems.append("write.kind must be file")
    else:
        problems.extend(_write_path_problems(str(write.get("path", "")), task.get("_profile", "")))
    report = task["report"]
    if not isinstance(report, dict) or not report.get("ok") or not report.get("fail"):
        problems.append("report.ok and report.fail are required (the checkable verdict)")
    retry = task.get("retry", {})
    if retry and (not isinstance(retry.get("max"), int) or retry["max"] < 0):
        problems.append("retry.max must be a non-negative integer")
    if not str(task["judge"]).strip():
        problems.append("judge must say what to decide")
    return problems


def render(task):
    """Expand the contract into the prompt a host cron job runs."""
    problems = validate(task)
    if problems:
        raise ValueError("; ".join(problems))
    collect = task["collect"]
    if collect["kind"] == "files":
        source = "读取这些文件（只读）：" + "、".join(collect["paths"])
    elif collect["kind"] == "command":
        source = "执行这条只读命令并看输出：" + collect["command"]
    else:
        source = "输入由会话上下文提供（只读）"
    retry = task.get("retry") or {}
    lines = [
        "[autotask:%s]" % task["id"],
        source + "。",
        "判断：" + str(task["judge"]).strip(),
        "把结果写入 %s（原子写，路径固定，不要写到别处）。" % task["write"]["path"],
        "回答两行：第一行严格是 `%s<一句判据>` 或 `%s<原因>`；第二行是需要人工处理时写下一步，否则写 `-`。"
        % (task["report"]["ok"], task["report"]["fail"]),
        "不要修改代码、配置或重启服务；判断不了就写 %s不确定。" % task["report"]["fail"],
    ]
    if task.get("notify"):
        lines.append(
            "【静默抑制】若判定为 %s且无需人工处理（第二行为 `-`），首行严格输出 [SILENT] 以抑制向用户推送无异常噪音。"
            % task["report"]["ok"]
        )
    if retry:
        lines.append("失败最多重试 %d 次，间隔 ≥%d 秒；超过后只告警，不再重试。"
                     % (retry["max"], retry.get("backoff_seconds", 60)))
    return "\n".join(lines)


def to_cron_job(task):
    job = {"name": task["id"], "schedule": task["schedule"], "prompt": render(task)}
    if "notify" in task:
        job["notify"] = bool(task["notify"])
    return job


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] not in {"validate", "render", "cron"}:
        print(__doc__, file=sys.stderr)
        return 2
    action, paths = argv[0], argv[1:]
    failed = 0
    for path in paths:
        task = load(path)
        if action == "validate":
            problems = validate(task)
            print(("OK   " if not problems else "FAIL ") + path + ("" if not problems else ": " + "; ".join(problems)))
            failed += bool(problems)
        elif action == "render":
            print(render(task))
        else:
            print(json.dumps([to_cron_job(task)], ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
