from __future__ import annotations

from pathlib import Path


AGENT_ROLES = {
    "frontend": {"label": "Frontend", "project_id": "frontend", "prompt": "frontend.md", "prompt_version": "1.0"},
    "backend": {"label": "Backend", "project_id": "backend", "prompt": "backend.md", "prompt_version": "1.0"},
    "python_ai": {"label": "Python/IA", "project_id": "python", "prompt": "python_ai.md", "prompt_version": "1.0"},
    "contracts": {"label": "Contracts", "project_id": "backend", "prompt": "contracts.md", "prompt_version": "1.0"},
    "qa": {"label": "QA", "project_id": "backend", "prompt": "qa.md", "prompt_version": "1.0"},
    "security": {"label": "Security/Cloud", "project_id": "backend", "prompt": "security.md", "prompt_version": "1.0"},
    "reviewer": {"label": "Code Reviewer", "project_id": "backend", "prompt": "reviewer.md", "prompt_version": "1.1"},
    "classifier": {"label": "Domain Classifier", "project_id": None, "prompt": "classifier.md", "prompt_version": "1.0"},
}

# Roles that judge an already-produced diff rather than writing code — lower reasoning effort
# is worth trying for these first since the existing quality-evaluation pipeline can measure
# whether it actually costs any quality. "qa" and "security" are deliberately excluded: despite
# having prompt files and a registry entry, run_tests/security_review (orchestrator/nodes.py)
# never dispatch them to Codex — they run deterministic checks (the project's own test command,
# a regex secret/permission scan) instead, so qa.md/security.md are currently unused prompts.
GATE_ROLES = {"contracts", "reviewer"}


def role_prompt(role: str, prompts_root: Path) -> str:
    path = prompts_root / AGENT_ROLES.get(role, {}).get("prompt", "supervisor.md")
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"Act as the {role} specialist. Work only in the assigned project and return structured evidence."
