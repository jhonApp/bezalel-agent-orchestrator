from __future__ import annotations

from orchestrator.routing import gates_pass


def test_gates_pass_blocks_when_a_task_hit_the_codex_usage_limit() -> None:
    """A rate_limited task never actually ran — it must block commit/merge/deploy exactly like
    a real failure, or the orchestrator would ship work some agents never got a chance to do."""
    state = {
        "plan": [{"task_id": "T001", "agent": "backend", "status": "rate_limited"}],
        "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
    }

    passed, reasons = gates_pass(state)

    assert passed is False
    assert reasons


def test_gates_pass_reports_a_distinct_reason_for_a_usage_limit_block() -> None:
    state = {
        "plan": [{"task_id": "T001", "agent": "backend", "status": "rate_limited"}],
        "contracts": [], "test_results": [], "review_results": [], "security_blocking": False,
    }

    _, reasons = gates_pass(state)

    assert any("usage limit" in r.lower() or "credit" in r.lower() for r in reasons)
