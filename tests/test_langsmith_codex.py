import json
from pathlib import Path

from observability.langsmith import LangSmithObserver
from orchestrator.config import Settings


def test_codex_trace_environment_has_execution_metadata_without_a_secret():
    root = Path(".").resolve()
    settings = Settings(
        orchestrator_root=root,
        workspace_root=root,
        frontend_path=root,
        backend_path=root,
        python_path=root,
        langsmith_codex_tracing=True,
        langsmith_codex_project="bezalel-observability",
    )

    with LangSmithObserver(settings).codex_session(
        "orchestrator.codex_task",
        {"role": "backend"},
        {"orchestrator_execution_id": "execution-123", "context_health": {"level": "healthy"}},
    ) as environment:
        assert environment["TRACE_TO_LANGSMITH"] == "true"
        assert environment["LANGSMITH_CODEX_PROJECT"] == "bezalel-observability"
        assert json.loads(environment["LANGSMITH_CODEX_METADATA"])["orchestrator_execution_id"] == "execution-123"
