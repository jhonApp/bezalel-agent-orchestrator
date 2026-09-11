from observability.context_health import ContextHealthMonitor
from orchestrator.config import Settings


def test_context_health_compacts_large_state(tmp_path):
    settings = Settings(orchestrator_root=tmp_path, workspace_root=tmp_path, frontend_path=tmp_path, backend_path=tmp_path, python_path=tmp_path)
    monitor = ContextHealthMonitor(settings)
    state = {"feature_request": "feature", "messages": ["x" * 5000] * 300, "errors": ["e"], "contracts": [{"id": 1}]}
    health = monitor.assess(state)
    assert health.level in {"warning", "critical"}
    compacted = monitor.compact({**state, "context_health": health.model_dump(mode="json")})
    assert compacted["context_compacted"] is True
    assert "context_summary" in compacted
