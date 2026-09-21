from __future__ import annotations

from typing import Any

from schemas.models import AgentResult, utc_now

_PARSE_FAILURE_ERROR = "structured output could not be parsed"
_DOMAIN_AGENTS = {"frontend", "backend", "python_ai"}


def _producing_task(project_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent task run by this project's exclusive domain owner (frontend/
    backend/python_ai) — never a gate agent like reviewer/contracts/security/qa, since
    those review the project rather than producing its diff.
    """
    matches = [
        t for t in state.get("plan", [])
        if t.get("project_id") == project_id and t.get("agent") in _DOMAIN_AGENTS
    ]
    return matches[-1] if matches else None


def heuristic_correta(project_id: str, state: dict[str, Any]) -> int | None:
    task = _producing_task(project_id, state)
    if task is None:
        return None
    if task.get("status") in ("failed", "blocked"):
        return 0
    test_results = [t for t in state.get("test_results", []) if t.get("project_id") == project_id]
    if any(t.get("status") == "failed" for t in test_results):
        return 0
    return 100


def heuristic_formato_valido(project_id: str, state: dict[str, Any]) -> int | None:
    task = _producing_task(project_id, state)
    if task is None:
        return None
    errors = (task.get("result") or {}).get("errors", [])
    return 0 if _PARSE_FAILURE_ERROR in errors else 100


def heuristic_seguranca(project_id: str, state: dict[str, Any]) -> int:
    blocking = [
        f for f in state.get("security_findings", [])
        if f.get("project_id") == project_id and f.get("severity") == "blocking"
    ]
    return 0 if blocking else 100


def blend_quality_score(axes: dict[str, int | None]) -> float | None:
    """Average every scored axis; a missing (``None``) axis is excluded, not treated as
    0. ``alucinacao`` is inverted first, since for it lower is better.
    """
    values: list[float] = []
    for key, value in axes.items():
        if value is None:
            continue
        values.append(100 - value if key == "alucinacao" else value)
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def build_quality_entry(project_id: str, state: dict[str, Any], agent_result: AgentResult) -> dict[str, Any]:
    """One quality_scores entry for a project the reviewer just examined: heuristic axes
    (tests/security/parse) blended with the reviewer's own LLM judgement (relevante/
    fonte_utilizada/alucinacao/cumprimento_regras).
    """
    task = _producing_task(project_id, state)
    result = (task.get("result") or {}) if task else {}
    quality = agent_result.quality or {}
    axes = {
        "correta": heuristic_correta(project_id, state),
        "relevante": quality.get("relevante"),
        "fonte_utilizada": quality.get("fonte_utilizada"),
        "alucinacao": quality.get("alucinacao"),
        "formato_valido": heuristic_formato_valido(project_id, state),
        "cumprimento_regras": quality.get("cumprimento_regras"),
        "seguranca": heuristic_seguranca(project_id, state),
    }
    return {
        "project_id": project_id,
        "agent": task.get("agent") if task else None,
        "prompt_version": result.get("prompt_version"),
        "axes": axes,
        "quality_score": blend_quality_score(axes),
        "estimated_cost": result.get("estimated_cost", 0.0),
        "duration_seconds": result.get("duration_seconds", 0.0),
        "computed_at": utc_now(),
    }
