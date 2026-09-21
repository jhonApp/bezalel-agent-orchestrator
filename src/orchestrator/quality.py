from __future__ import annotations

from typing import Any

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
