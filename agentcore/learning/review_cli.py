#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选审查 CLI：list / show / approve / reject / revoke。

为什么需要一层 CLI
------------------
``CandidateStore`` 已经提供审查门禁，但没有**可操作的人机入口**：审查者需要用
一条命令看到待审队列、检查证据、给出具名结论，并在上游更正后撤回已批准的候选。
本模块只做参数解析与输出格式，所有状态变更都走 ``CandidateStore`` 的既有 API：

    list   → CandidateStore.list(status=..., domain=...)
    show   → CandidateStore.get(...)
    approve/reject → CandidateStore.review(decision=approve|reject)
    revoke → CandidateStore.revoke(...)（仅 approved，理由必填）

安全边界
--------
- ``--root`` 必填：CLI **没有**默认候选库路径，避免误触生产数据。
- 审查者必填且由存储层再校验：不能是空串、自动化身份或候选作者本人。
- reject / revoke 的理由必填：没有理由的否决/撤销不可审计。
- 退出码固定：0 成功，1 业务拒绝（未知候选、门禁挡下、终态重复审查等），
  2 用法错误（argparse / 缺 ``--root``），3 存储读写失败。
- 重复操作是显式拒绝而不是静默成功：撤销后的候选仍是终态，再次
  approve/reject/revoke 都会返回 1，状态保持不变。

用法::

    python -m agentcore.learning.review_cli --root DIR list --status pending
    python -m agentcore.learning.review_cli --root DIR show <candidate_id>
    python -m agentcore.learning.review_cli --root DIR approve <id> \\
        --reviewer alice --reason "逐字引文复核通过"
    python -m agentcore.learning.review_cli --root DIR reject <id> \\
        --reviewer alice --reason "来源已过期"
    python -m agentcore.learning.review_cli --root DIR revoke <id> \\
        --reviewer alice --reason "上游发布更正"

``--root`` 与 ``--json`` 放在子命令前后均可（子命令后的值优先）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence, TextIO, Tuple

from .candidates import (
    DECISION_APPROVE,
    DECISION_REJECT,
    STATUSES,
    Candidate,
    CandidateError,
    CandidateStore,
)

__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "EXIT_INTERNAL",
    "build_parser",
    "main",
]

#: 成功（包括列表为空）。
EXIT_OK = 0
#: 业务拒绝：未知候选、审批门禁、终态重复审查、理由/审查者不合格。
EXIT_REFUSED = 1
#: 用法错误：argparse 解析失败或缺少必填 ``--root``。
EXIT_USAGE = 2
#: 候选库读写失败（权限、磁盘、损坏之外的 IO 层问题）。
EXIT_INTERNAL = 3

_LIST_CLAIM_CHARS = 80


class _Refusal(Exception):
    """CLI 自身的业务拒绝（存储层异常之外的参数语义检查）。"""


# ── 参数解析 ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agentcore.learning.review_cli",
        description="候选知识审查 CLI：查看待审队列、审批、否决与撤销。",
    )
    # 顶层与子命令都接受这两个选项；子命令用 SUPPRESS 默认值，避免覆盖顶层值。
    parser.add_argument("--root", metavar="DIR",
                        help="候选库根目录（必填；没有默认路径）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", metavar="DIR", default=argparse.SUPPRESS,
                        help="候选库根目录（也可放在子命令前）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出 JSON")

    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p_list = sub.add_parser("list", parents=[common],
                            help="列出候选（可按状态/域过滤）")
    p_list.add_argument("--status", choices=STATUSES, help="按状态过滤")
    p_list.add_argument("--domain", help="按 domain/scope 过滤")
    p_list.set_defaults(handler=_cmd_list)

    p_show = sub.add_parser("show", parents=[common], help="查看单条候选证据")
    p_show.add_argument("candidate_id")
    p_show.set_defaults(handler=_cmd_show)

    for name, help_text, handler, reason_required in (
        ("approve", "批准候选（走 CandidateStore.review）", _cmd_approve, False),
        ("reject", "否决候选（理由必填）", _cmd_reject, True),
        ("revoke", "撤销已批准候选（理由必填）", _cmd_revoke, True),
    ):
        command = sub.add_parser(name, parents=[common], help=help_text)
        command.add_argument("candidate_id")
        command.add_argument("--reviewer", required=True,
                             help="具名人类审查者（不得是候选作者本人）")
        command.add_argument("--reason", required=reason_required,
                             default=None if reason_required else "",
                             help="审查/撤销理由（reject 与 revoke 必填）")
        command.set_defaults(handler=handler)

    return parser


# ── 命令实现 ────────────────────────────────────────────────────────────────
def _cmd_list(store: CandidateStore, args) -> Tuple[object, List[str]]:
    candidates = store.list(status=args.status, domain=args.domain)
    return [c.to_dict() for c in candidates], [_list_line(c) for c in candidates]


def _cmd_show(store: CandidateStore, args) -> Tuple[object, List[str]]:
    candidate = store.get(args.candidate_id)
    return candidate.to_dict(), list(_detail_lines(candidate))


def _cmd_approve(store: CandidateStore, args) -> Tuple[object, List[str]]:
    reviewer = _require_reviewer(args.reviewer)
    note = (args.reason or "").strip()
    updated = store.review(args.candidate_id, reviewer=reviewer,
                           decision=DECISION_APPROVE, note=note)
    return _action_payload("approve", updated)


def _cmd_reject(store: CandidateStore, args) -> Tuple[object, List[str]]:
    reviewer = _require_reviewer(args.reviewer)
    reason = _require_reason(args.reason)
    updated = store.review(args.candidate_id, reviewer=reviewer,
                           decision=DECISION_REJECT, note=reason)
    return _action_payload("reject", updated)


def _cmd_revoke(store: CandidateStore, args) -> Tuple[object, List[str]]:
    reviewer = _require_reviewer(args.reviewer)
    reason = _require_reason(args.reason)
    updated = store.revoke(args.candidate_id, reviewer=reviewer, reason=reason)
    return _action_payload("revoke", updated)


# ── 输出格式 ────────────────────────────────────────────────────────────────
def _list_line(candidate: Candidate) -> str:
    claim = candidate.subject or candidate.statement
    return "%s\t%s\t%s\t%s\t%s" % (
        candidate.candidate_id,
        candidate.review_status,
        candidate.domain,
        candidate.reviewer or "-",
        _short(claim),
    )


def _detail_lines(candidate: Candidate) -> Sequence[str]:
    return (
        "candidate_id: %s" % candidate.candidate_id,
        "status: %s" % candidate.review_status,
        "domain: %s" % candidate.domain,
        "author: %s" % (candidate.author or "-"),
        "reviewer: %s" % (candidate.reviewer or "-"),
        "reviewed_at: %s" % (candidate.reviewed_at or "-"),
        "review_note: %s" % (candidate.review_note or "-"),
        "subject: %s" % (candidate.subject or "-"),
        "statement: %s" % candidate.statement,
        "literal_quote: %s" % candidate.literal_quote,
        "source_url: %s" % candidate.source_url,
        "source_hash: %s" % candidate.source_hash,
        "source_id: %s" % candidate.source_id,
        "retrieved_at: %s" % candidate.retrieved_at,
    )


def _action_payload(command: str, candidate: Candidate) -> Tuple[object, List[str]]:
    payload = {"command": command, "candidate": candidate.to_dict()}
    line = "%s %s reviewer=%s at=%s" % (
        candidate.review_status,
        candidate.candidate_id,
        candidate.reviewer or "-",
        candidate.reviewed_at or "-",
    )
    return payload, [line]


def _short(text: str) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= _LIST_CLAIM_CHARS:
        return collapsed
    return collapsed[: _LIST_CLAIM_CHARS - 3] + "..."


# ── 参数语义校验 ────────────────────────────────────────────────────────────
def _require_reviewer(raw: Optional[str]) -> str:
    reviewer = (raw or "").strip()
    if not reviewer:
        raise _Refusal("reviewer 不能为空：审查必须留下具名人工身份")
    return reviewer


def _require_reason(raw: Optional[str]) -> str:
    reason = (raw or "").strip()
    if not reason:
        raise _Refusal("reason 不能为空：reject/revoke 必须留下可审计的理由")
    return reason


# ── 入口 ────────────────────────────────────────────────────────────────────
def main(argv: Optional[Sequence[str]] = None, *, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None) -> int:
    """执行一次 CLI 调用并返回退出码；``stdout``/``stderr`` 可注入以便测试。"""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    parser = build_parser()
    try:
        # argparse 的 --help/解析失败以 SystemExit 结束；这里转成退出码，
        # 让调用方（测试、上层脚本）不需要捕获异常。
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0)

    root = getattr(args, "root", None)
    json_mode = bool(getattr(args, "json", False))
    if not root:
        _emit(err, "error: --root 必填：候选库必须显式指定，CLI 不会回退到生产路径")
        return EXIT_USAGE

    try:
        store = CandidateStore(root)
    except OSError as exc:
        _emit(err, "error: 候选库无法打开：%s" % exc)
        return EXIT_INTERNAL

    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse 已要求子命令
        _emit(err, "error: 缺少子命令")
        return EXIT_USAGE

    try:
        payload, lines = handler(store, args)
    except _Refusal as exc:
        _emit(err, "error: %s" % exc)
        return EXIT_REFUSED
    except CandidateError as exc:
        _emit(err, "error: %s" % exc)
        return EXIT_REFUSED
    except OSError as exc:
        _emit(err, "error: 候选库读写失败：%s" % exc)
        return EXIT_INTERNAL

    if json_mode:
        _emit(out, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    elif lines:
        _emit(out, "\n".join(lines))
    return EXIT_OK


def _emit(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())
