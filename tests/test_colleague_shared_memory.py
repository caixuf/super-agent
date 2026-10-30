"""The existing memory store's team boundary, without a live profile."""

from agentcore.memory import memory_store, runtime_bridge


def test_team_members_share_facts_but_other_teams_do_not(tmp_path, configured):
    # The host, not the model, must bind this same user_id for all team members.
    with configured(tmp_path):
        fact = memory_store.write_fact(
            "团队决定门限为 3.0%", subject="门限", user_id="team-a",
            agent_id="alice", source="manual",
        )
        assert fact["written"] is True
        assert memory_store.flush_background(timeout=30) == 0
        # The next member's new turn uses the existing automatic-recall bridge.
        shared = runtime_bridge.prefetch_context("团队决定门限为 3.0%", user_id="team-a")
        outsider = runtime_bridge.prefetch_context("团队决定门限为 3.0%", user_id="team-b")
        assert shared["ok"] and fact["fact_id"] in [f["fact_id"] for f in shared["facts"]]
        assert outsider["ok"] and outsider["count"] == 0
