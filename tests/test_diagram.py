import json
from pathlib import Path


def test_excalidraw_is_valid_and_contains_required_nodes():
    path = Path(__file__).parents[1] / "docs/architecture/agent-orchestration.excalidraw"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["type"] == "excalidraw"
    text = " ".join(e.get("text", "") for e in data["elements"])
    for required in ("Tech Lead", "Frontend", "Backend", "Python / IA", "Codex CLI", "LangSmith", "deploy"):
        assert required in text
