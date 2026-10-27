#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审查 CLI + 撤销流程的离线验收。

约束：候选库落在 ``tmp_path``；不联网、不写正式记忆库、不触碰生产路径。
所有断言都通过 CLI 退出码/输出 **以及** 重开候选库后的持久化状态来验证。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcore.learning import (
    STATUS_APPROVED,
    STATUS_CONFLICTED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_REVOKED,
    CandidateStore,
)
from agentcore.learning.promotion import CandidateNotPromotable, promote_candidate
from agentcore.learning.review_cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    main,
)

SNAPSHOT = (
    "Widget 4.2 release notes\n"
    "Widget 4.2 was released on 2026-03-04.\n"
    "The widget firmware can be\n"
    "updated over the air.\n"
)
QUOTE = "Widget 4.2 was released on 2026-03-04."
URL = "https://example.test/widget-4.2/release-notes"
RETRIEVED = "2026-10-20T09:30:00Z"
SUBJECT = "widget-4.2-release-date"
REVIEWER = "reviewer-human"

SNAPSHOT_APRIL = "Widget 4.2 shipped in April 2026.\nMaintenance window: Fridays.\n"
QUOTE_APRIL = "Widget 4.2 shipped in April 2026."
URL_APRIL = "https://example.test/widget-4.2/april"


class RecordingMemory:
    """同形状假记忆库：撤销后的晋升必须**在写入前**被拒，因此不应有任何调用。"""

    def __init__(self):
        self.calls = []

    def write_fact(self, statement, **kwargs):
        self.calls.append({"statement": statement, "kwargs": kwargs})
        return {"ok": True, "written": True,
                "fact_id": "candfact_fake", "tags": kwargs.get("tags", {})}


def make_store(root):
    return CandidateStore(root)


def ingest(store, **overrides):
    kwargs = dict(
        statement=QUOTE,
        literal_quote=QUOTE,
        source_url=URL,
        retrieved_at=RETRIEVED,
        domain="Reference",
        snapshot=SNAPSHOT,
        subject=SUBJECT,
        author="retriever-1",
    )
    kwargs.update(overrides)
    return store.ingest(**kwargs).candidate


def run(root, *argv):
    """执行 CLI（``--root`` 在前），返回 (exit_code, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    code = main(["--root", str(root), *argv], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ── 1. list / show ──────────────────────────────────────────────────────────
def test_list_pending_excludes_reviewed_and_show_reports_evidence(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store, subject="alpha")
    second = ingest(store, subject="beta")
    approved = ingest(store, subject="gamma")
    store.review(approved.candidate_id, reviewer=REVIEWER, decision="approve")

    code, out, err = run(tmp_path, "list", "--status", "pending")
    assert (code, err) == (EXIT_OK, "")
    assert first.candidate_id in out and second.candidate_id in out
    assert approved.candidate_id not in out
    assert out.count("pending") == 2

    code, out, _ = run(tmp_path, "--json", "list", "--status", "pending")
    assert code == EXIT_OK
    rows = json.loads(out)
    assert {row["candidate_id"] for row in rows} == {first.candidate_id,
                                                     second.candidate_id}
    assert all(row["review_status"] == STATUS_PENDING for row in rows)

    code, out, err = run(tmp_path, "show", first.candidate_id)
    assert (code, err) == (EXIT_OK, "")
    assert "status: pending" in out
    assert QUOTE in out
    assert URL in out

    code, out, _ = run(tmp_path, "--json", "show", first.candidate_id)
    assert code == EXIT_OK
    shown = json.loads(out)
    assert shown["candidate_id"] == first.candidate_id
    assert shown["literal_quote"] == QUOTE

    code, out, err = run(tmp_path, "show", "cand_missing")
    assert code == EXIT_REFUSED and out == ""
    assert "未知候选" in err


def test_options_accepted_after_subcommand(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)

    out, err = io.StringIO(), io.StringIO()
    code = main(["list", "--root", str(tmp_path), "--json",
                 "--status", "pending"], stdout=out, stderr=err)
    assert (code, err.getvalue()) == (EXIT_OK, "")
    assert [row["candidate_id"] for row in json.loads(out.getvalue())] == [
        candidate.candidate_id]


def test_list_filters_by_domain_and_rejects_invalid_domain(tmp_path):
    store = make_store(tmp_path)
    reference = ingest(store, subject="ref", domain="Reference")
    ops = ingest(store, subject="ops", domain="ops-runbook")

    code, out, _ = run(tmp_path, "list", "--domain", "reference")
    assert code == EXIT_OK
    assert reference.candidate_id in out and ops.candidate_id not in out

    code, _, err = run(tmp_path, "list", "--domain", "not a domain")
    assert code == EXIT_REFUSED
    assert "domain" in err


# ── 2. approve / reject ─────────────────────────────────────────────────────
def test_approve_goes_through_review_api_and_records_note(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)
    reason = "逐字引文复核通过"

    code, out, err = run(tmp_path, "approve", candidate.candidate_id,
                         "--reviewer", REVIEWER, "--reason", reason)
    assert (code, err) == (EXIT_OK, "")
    assert candidate.candidate_id in out and STATUS_APPROVED in out

    reloaded = make_store(tmp_path)
    stored = reloaded.get(candidate.candidate_id)
    assert stored.review_status == STATUS_APPROVED
    assert stored.reviewer == REVIEWER
    assert stored.review_note == reason
    assert stored.is_confirmed_fact is False
    assert [(row["decision"], row["outcome"], row["reviewer"])
            for row in reloaded.attempts(candidate.candidate_id)] == [
        ("approve", "approved", REVIEWER)]


def test_reject_records_mandatory_reason(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)
    reason = "来源已过期，不得入库"

    code, out, err = run(tmp_path, "reject", candidate.candidate_id,
                         "--reviewer", REVIEWER, "--reason", reason)
    assert (code, err) == (EXIT_OK, "")
    assert STATUS_REJECTED in out

    reloaded = make_store(tmp_path)
    stored = reloaded.get(candidate.candidate_id)
    assert stored.review_status == STATUS_REJECTED
    assert stored.reviewer == REVIEWER
    assert stored.review_note == reason
    assert reloaded.attempts(candidate.candidate_id)[-1]["decision"] == "reject"


def test_reject_and_revoke_require_a_non_blank_reason(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)

    code, _, _ = run(tmp_path, "reject", candidate.candidate_id,
                     "--reviewer", REVIEWER)
    assert code == EXIT_USAGE

    code, _, err = run(tmp_path, "reject", candidate.candidate_id,
                       "--reviewer", REVIEWER, "--reason", "   ")
    assert code == EXIT_REFUSED and "reason" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING
    assert make_store(tmp_path).attempts(candidate.candidate_id) == ()


def test_approve_requires_a_named_reviewer(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)

    code, _, _ = run(tmp_path, "approve", candidate.candidate_id)
    assert code == EXIT_USAGE

    code, _, err = run(tmp_path, "approve", candidate.candidate_id,
                       "--reviewer", "   ")
    assert code == EXIT_REFUSED and "reviewer" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING


def test_self_review_is_refused_for_approve_and_reject(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store, author=REVIEWER)

    code, _, err = run(tmp_path, "approve", candidate.candidate_id,
                       "--reviewer", REVIEWER, "--reason", "自查")
    assert code == EXIT_REFUSED and "独立审查" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING
    assert make_store(tmp_path).attempts(candidate.candidate_id) == ()

    code, _, err = run(tmp_path, "reject", candidate.candidate_id,
                       "--reviewer", REVIEWER, "--reason", "自查否决")
    assert code == EXIT_REFUSED and "独立审查" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING
    assert make_store(tmp_path).attempts(candidate.candidate_id) == ()


def test_automated_reviewer_is_refused(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)

    code, _, err = run(tmp_path, "approve", candidate.candidate_id,
                       "--reviewer", "auto")
    assert code == EXIT_REFUSED and "自动化身份" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING


def test_approve_is_blocked_by_unresolved_conflict(tmp_path):
    store = make_store(tmp_path)
    first = ingest(store)
    ingest(store, statement=QUOTE_APRIL, literal_quote=QUOTE_APRIL,
           source_url=URL_APRIL, snapshot=SNAPSHOT_APRIL)

    code, _, err = run(tmp_path, "approve", first.candidate_id,
                       "--reviewer", REVIEWER)
    assert code == EXIT_REFUSED and "conflicting_evidence" in err

    reloaded = make_store(tmp_path)
    assert reloaded.get(first.candidate_id).review_status == STATUS_CONFLICTED
    assert reloaded.attempts(first.candidate_id)[-1]["outcome"] == "blocked"


# ── 3. revoke ───────────────────────────────────────────────────────────────
def test_revoked_candidate_records_reason_and_cannot_promote(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)
    store.review(candidate.candidate_id, reviewer=REVIEWER, decision="approve")
    reason = "上游发布更正，原结论作废"

    code, out, err = run(tmp_path, "revoke", candidate.candidate_id,
                         "--reviewer", REVIEWER, "--reason", reason)
    assert (code, err) == (EXIT_OK, "")
    assert STATUS_REVOKED in out

    reloaded = make_store(tmp_path)
    stored = reloaded.get(candidate.candidate_id)
    assert stored.review_status == STATUS_REVOKED
    assert stored.reviewer == REVIEWER
    assert stored.review_note == reason
    assert stored.is_terminal is True
    assert stored.is_confirmed_fact is False
    assert [row["decision"] for row in reloaded.attempts(candidate.candidate_id)] == [
        "approve", "revoke"]

    memory = RecordingMemory()
    with pytest.raises(CandidateNotPromotable):
        promote_candidate(reloaded, candidate.candidate_id,
                          reviewer="promoter-human", memory_store=memory)
    assert memory.calls == []


def test_revoke_requires_approved_status(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)

    code, _, err = run(tmp_path, "revoke", candidate.candidate_id,
                       "--reviewer", REVIEWER, "--reason", "撤销未批准候选")
    assert code == EXIT_REFUSED and "只有 approved" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_PENDING
    assert make_store(tmp_path).attempts(candidate.candidate_id) == ()

    code, _, _ = run(tmp_path, "revoke", candidate.candidate_id,
                     "--reviewer", REVIEWER)
    assert code == EXIT_USAGE


def test_revoke_rejects_the_candidate_author(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store, author=REVIEWER)
    store.review(candidate.candidate_id, reviewer="other-human", decision="approve")

    code, _, err = run(tmp_path, "revoke", candidate.candidate_id,
                       "--reviewer", REVIEWER, "--reason", "作者自撤")
    assert code == EXIT_REFUSED and "同一身份" in err
    assert make_store(tmp_path).get(candidate.candidate_id).review_status == STATUS_APPROVED


# ── 4. 重复/幂等语义 ────────────────────────────────────────────────────────
def test_repeat_actions_are_refused_and_state_is_unchanged(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)
    cid = candidate.candidate_id
    approve = ("approve", cid, "--reviewer", REVIEWER, "--reason", "复核")
    revoke = ("revoke", cid, "--reviewer", REVIEWER, "--reason", "更正")

    assert run(tmp_path, *approve)[0] == EXIT_OK
    before = make_store(tmp_path).get(cid)

    code, _, err = run(tmp_path, *approve)
    assert code == EXIT_REFUSED and "已终结" in err
    assert make_store(tmp_path).get(cid) == before

    code, _, err = run(tmp_path, "reject", cid, "--reviewer", REVIEWER,
                       "--reason", "重复否决")
    assert code == EXIT_REFUSED and "已终结" in err

    assert run(tmp_path, *revoke)[0] == EXIT_OK
    code, _, err = run(tmp_path, *revoke)
    assert code == EXIT_REFUSED and "已终结" in err

    code, _, err = run(tmp_path, *approve)
    assert code == EXIT_REFUSED and "已终结" in err

    reloaded = make_store(tmp_path)
    assert reloaded.get(cid).review_status == STATUS_REVOKED
    assert [row["decision"] for row in reloaded.attempts(cid)] == ["approve", "revoke"]


def test_repeated_reject_is_refused(tmp_path):
    store = make_store(tmp_path)
    candidate = ingest(store)
    reject = ("reject", candidate.candidate_id, "--reviewer", REVIEWER,
              "--reason", "过期")

    assert run(tmp_path, *reject)[0] == EXIT_OK
    code, _, err = run(tmp_path, *reject)
    assert code == EXIT_REFUSED and "已终结" in err
    assert make_store(tmp_path).attempts(candidate.candidate_id)[-1]["decision"] == "reject"
    assert len(make_store(tmp_path).attempts(candidate.candidate_id)) == 1


# ── 5. 用法与模块入口 ───────────────────────────────────────────────────────
def test_root_is_mandatory_and_unknown_options_are_usage_errors(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert main(["list"], stdout=out, stderr=err) == EXIT_USAGE
    assert "--root" in err.getvalue()

    sink_out, sink_err = io.StringIO(), io.StringIO()
    assert main(["--root", str(tmp_path), "list", "--status", "bogus"],
                stdout=sink_out, stderr=sink_err) == EXIT_USAGE
    assert main(["--root", str(tmp_path), "bogus"],
                stdout=sink_out, stderr=sink_err) == EXIT_USAGE


def test_module_entrypoint_help():
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    result = subprocess.run(
        [sys.executable, "-m", "agentcore.learning.review_cli", "--help"],
        cwd=repo_root, capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0
    assert "--root" in result.stdout
    assert "revoke" in result.stdout
