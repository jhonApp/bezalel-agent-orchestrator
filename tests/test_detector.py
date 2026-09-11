from pathlib import Path

from adapters.project_detector import discover_projects
from types import SimpleNamespace


def test_detector_identifies_three_stacks(tmp_path: Path):
    front = tmp_path / "bezalel-app"
    front.mkdir()
    (front / "package.json").write_text('{"dependencies":{"react":"18"},"devDependencies":{"vite":"5","typescript":"5","vitest":"1"},"scripts":{"build":"vite build","test":"vitest run"}}', encoding="utf-8")
    (front / "package-lock.json").write_text("{}", encoding="utf-8")
    back = tmp_path / "Bezalel"
    back.mkdir()
    (back / "Bezalel.sln").write_text("", encoding="utf-8")
    (back / "Api.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>', encoding="utf-8")
    py = tmp_path / "Workflow-IA"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]\ndependencies=['langgraph']", encoding="utf-8")
    found = {item.project_id: item for item in discover_projects(SimpleNamespace(frontend_path=front, backend_path=back, python_path=py))}
    assert found["frontend"].framework == ["React", "Vite", "TypeScript", "Vitest"]
    assert found["backend"].language == "C#"
    assert "LangGraph" in found["python"].framework
