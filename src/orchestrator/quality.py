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


def quality_by_execution(states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for execution_id, state in states.items():
        for entry in state.get("quality_scores", []):
            rows.append({"execution_id": execution_id, **entry})
    rows.sort(key=lambda r: r.get("computed_at", ""), reverse=True)
    return rows


def quality_by_version(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        agent, version = row.get("agent"), row.get("prompt_version")
        if not agent or not version:
            continue
        grouped.setdefault((agent, version), []).append(row)
    result = []
    for (agent, version), items in grouped.items():
        scores = [r["quality_score"] for r in items if r.get("quality_score") is not None]
        result.append({
            "agent": agent, "prompt_version": version, "runs": len(items),
            "avg_quality": round(sum(scores) / len(scores), 1) if scores else None,
            "avg_cost_usd": round(sum(r.get("estimated_cost", 0.0) for r in items) / len(items), 6),
            "avg_duration_seconds": round(sum(r.get("duration_seconds", 0.0) for r in items) / len(items), 1),
        })
    result.sort(key=lambda item: (item["agent"], item["prompt_version"]))
    return result
