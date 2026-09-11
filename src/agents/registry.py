from __future__ import annotations

from pathlib import Path


AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md"},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md"},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md"},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md"},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md"},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md"},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md"},
}


def role_prompt(role: str, prompts_root: Path) -> str:
    path = prompts_root / AGENT_ROLES.get(role, {}).get("prompt", "supervisor.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"Act as the {role} specialist. Work only in the assigned project and return structured evidence."
