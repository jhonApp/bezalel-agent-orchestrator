from __future__ import annotations

from orchestrator.quality import (
    blend_quality_score,
    heuristic_correta,
    heuristic_formato_valido,
    heuristic_seguranca,
)


def test_heuristic_correta_is_100_when_task_completed_and_tests_passed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed"}],
        "test_results": [{"project_id": "frontend", "status": "passed"}],
    }
    assert heuristic_correta("frontend", state) == 100


def test_heuristic_correta_is_0_when_a_test_failed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed"}],
        "test_results": [{"project_id": "frontend", "status": "failed"}],
    }
    assert heuristic_correta("frontend", state) == 0


def test_heuristic_correta_is_0_when_the_producing_task_itself_failed():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "failed"}],
        "test_results": [],
    }
    assert heuristic_correta("frontend", state) == 0


def test_heuristic_correta_is_none_when_no_domain_task_produced_that_project():
    state = {"plan": [], "test_results": []}
    assert heuristic_correta("frontend", state) is None


def test_heuristic_correta_ignores_gate_agents_like_reviewer_and_contracts():
    state = {
        "plan": [{"agent": "reviewer", "project_id": "frontend", "status": "failed"}],
        "test_results": [],
    }
    assert heuristic_correta("frontend", state) is None


def test_heuristic_formato_valido_is_0_when_codex_output_failed_to_parse():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "failed",
                  "result": {"errors": ["structured output could not be parsed"]}}],
    }
    assert heuristic_formato_valido("frontend", state) == 0


def test_heuristic_formato_valido_is_100_when_parse_succeeded():
    state = {
        "plan": [{"agent": "frontend", "project_id": "frontend", "status": "completed",
                  "result": {"errors": []}}],
    }
    assert heuristic_formato_valido("frontend", state) == 100


def test_heuristic_seguranca_is_0_when_a_blocking_finding_belongs_to_this_project():
    state = {"security_findings": [{"project_id": "frontend", "severity": "blocking"}]}
    assert heuristic_seguranca("frontend", state) == 0


def test_heuristic_seguranca_ignores_blocking_findings_from_other_projects():
    state = {"security_findings": [{"project_id": "backend", "severity": "blocking"}]}
    assert heuristic_seguranca("frontend", state) == 100


def test_blend_quality_score_inverts_alucinacao_and_ignores_missing_axes():
    axes = {"correta": 100, "relevante": 80, "fonte_utilizada": None, "alucinacao": 10,
            "formato_valido": 100, "cumprimento_regras": 90, "seguranca": 100}
    # fonte_utilizada excluded (None); alucinacao inverted 10 -> 90.
    # (100 + 80 + 90 + 100 + 90 + 100) / 6 = 93.333... -> rounds to 93.3
    assert blend_quality_score(axes) == 93.3


def test_blend_quality_score_is_none_when_every_axis_is_missing():
    assert blend_quality_score({"correta": None, "alucinacao": None}) is None
