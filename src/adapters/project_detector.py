from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from orchestrator.config import Settings
from schemas.models import ProjectDetection


IGNORE_DIRS = {".git", "node_modules", "bin", "obj", "dist", ".vite", "venv", "__pycache__", ".pytest_cache", "cdk.out"}


def _files(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob(pattern) if not any(part in IGNORE_DIRS for part in p.parts)]


def _relative(paths: Iterable[Path], root: Path) -> list[str]:
    return [str(p.relative_to(root)).replace("\\", "/") for p in paths]


def _read(path: Path, limit: int = 300_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _scripts(package: dict) -> dict[str, str]:
    return {str(k): str(v) for k, v in package.get("scripts", {}).items()}


def detect_frontend(root: Path, expected_name: str = "bezalel-frontend") -> ProjectDetection:
    manifest = root / "package.json"
    if not manifest.exists():
        return ProjectDetection(
            project_id="frontend", expected_name=expected_name, path=str(root), exists=False,
            risks=["package.json not found; frontend stack cannot be detected"],
        )
    try:
        package = json.loads(_read(manifest))
    except json.JSONDecodeError as exc:
        return ProjectDetection(project_id="frontend", expected_name=expected_name, path=str(root), exists=True,
                                 risks=[f"invalid package.json: {exc}"])
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    framework: list[str] = []
    checks = [("react", "React"), ("next", "Next.js"), ("vite", "Vite"), ("typescript", "TypeScript"),
              ("tailwindcss", "Tailwind"), ("vitest", "Vitest"), ("jest", "Jest")]
    for dep, label in checks:
        if dep in deps:
            framework.append(label)
    lockfiles = [name for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock")
                 if (root / name).exists()]
    managers = {"package-lock.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun", "bun.lock": "bun"}
    manager = managers.get(lockfiles[0]) if lockfiles else None
    risks: list[str] = []
    if len(lockfiles) > 1:
        risks.append(f"multiple package manager lockfiles detected: {', '.join(lockfiles)}")
    scripts = _scripts(package)
    commands = {k: (f"{manager} run {v}" if manager else v) for k, v in scripts.items()
                if k in {"dev", "build", "lint", "test", "test:coverage", "typecheck", "deploy"}}
    deploy = []
    if (root / "vercel.json").exists():
        deploy.append("Vercel (vercel.json)")
    if (root / ".github" / "workflows").exists():
        deploy.append("GitHub Actions")
    if "deploy" in scripts:
        deploy.append(f"package script: {commands.get('deploy', scripts['deploy'])}")
    return ProjectDetection(
        project_id="frontend", expected_name=expected_name, path=str(root), exists=True,
        language="TypeScript" if "TypeScript" in framework else "JavaScript",
        framework=framework, package_manager=manager, dependency_manifests=["package.json"], lockfiles=lockfiles,
        commands=commands,
        important_files=_relative([manifest, *[root / n for n in ("vite.config.ts", "next.config.js", "tailwind.config.ts", "vercel.json") if (root / n).exists()]], root),
        test_strategy="Vitest" if "Vitest" in framework else ("Jest" if "Jest" in framework else "script-based tests"),
        deploy_strategy=deploy, risks=risks,
        evidence=[f"dependencies: {', '.join(sorted(deps))}", f"scripts: {', '.join(sorted(scripts))}"],
    )


def detect_backend(root: Path, expected_name: str = "bezalel-backend") -> ProjectDetection:
    solutions = _files(root, "*.sln")
    projects = _files(root, "*.csproj")
    if not solutions and not projects:
        return ProjectDetection(project_id="backend", expected_name=expected_name, path=str(root), exists=False,
                                 risks=["no .sln or .csproj found; .NET stack cannot be detected"])
    project_text = "\n".join(_read(p) for p in projects)
    frameworks = sorted(set(re.findall(r"<TargetFrameworks?>([^<]+)", project_text)))
    package_names = set(re.findall(r'<PackageReference\s+Include="([^"]+)"', project_text, re.I))
    integrations = [label for marker, label in (("AWSSDK.DynamoDBv2", "AWS DynamoDB"), ("AWSSDK.SQS", "AWS SQS"),
                  ("AWSSDK.S3", "AWS S3"), ("Amazon.Lambda", "AWS Lambda"), ("Amazon.CDK", "AWS CDK"),
                  ("Cognito", "Cognito"), ("ApiGateway", "API Gateway")) if any(marker.lower() in x.lower() for x in package_names | {project_text})]
    workflow_files = _files(root / ".github", "*.yml") + _files(root / ".github", "*.yaml")
    deploy = []
    if (root / "Bezalel.Infrastructure.Iac" / "cdk.json").exists() or "Amazon.CDK" in project_text:
        deploy.append("AWS CDK")
    if workflow_files:
        deploy.append("GitHub Actions")
    dockerfiles = _files(root, "Dockerfile*")
    if dockerfiles:
        deploy.append("Docker")
    scripts = {p.name: "file" for p in _files(root, "*.ps1") + _files(root, "*.sh")}
    tests = [p for p in projects if "test" in p.name.lower() or "test" in str(p.parent).lower()]
    return ProjectDetection(
        project_id="backend", expected_name=expected_name, path=str(root), exists=True, language="C#",
        framework=[".NET " + f for f in frameworks] or [".NET (TargetFramework not parsed)"],
        dependency_manifests=_relative(solutions + projects, root), lockfiles=[],
        commands={"restore": "dotnet restore", "build": "dotnet build", "test": "dotnet test"},
        important_files=_relative(solutions[:3] + projects[:20] + dockerfiles[:10] + workflow_files[:10], root),
        test_strategy=f"dotnet test; {len(tests)} test project(s) detected", deploy_strategy=deploy,
        integrations=integrations, existing_agents_or_workflows=_relative(workflow_files, root),
        risks=["multiple solution/project entry points; select the affected project explicitly"] if len(solutions) > 1 else [],
        evidence=[f"target frameworks: {', '.join(frameworks) or 'not found'}", f"packages: {', '.join(sorted(package_names))}"],
    )


def detect_python(root: Path, expected_name: str = "bezalel-python") -> ProjectDetection:
    manifests = [p for p in (root / "pyproject.toml", root / "requirements.txt", root / "requirements-dev.txt",
                             root / "poetry.lock", root / "uv.lock", root / "Pipfile") if p.exists()]
    sources = _files(root, "*.py")
    if not manifests and not sources:
        return ProjectDetection(project_id="python", expected_name=expected_name, path=str(root), exists=False,
                                 risks=["no Python manifest or source file found"])
    all_text = "\n".join(_read(p, 100_000) for p in sources[:100])
    deps_text = "\n".join(_read(p) for p in manifests)
    combined = (all_text + "\n" + deps_text).lower()
    framework = [label for marker, label in (("langgraph", "LangGraph"), ("langchain", "LangChain"),
                  ("gemini", "Gemini"), ("anthropic", "Anthropic"), ("openai", "OpenAI"),
                  ("boto3", "AWS boto3"), ("lambda", "AWS Lambda")) if marker in combined]
    workflows = _files(root / ".github", "*.yml") + _files(root / ".github", "*.yaml")
    scripts = _files(root, "*.ps1") + _files(root, "*.sh") + _files(root, "Makefile")
    deploy = ["GitHub Actions"] if workflows else []
    if (root / "Dockerfile.build").exists() or (root / "Dockerfile").exists():
        deploy.append("Docker")
    if scripts:
        deploy.append("deployment scripts")
    commands = {"test": "python -m pytest", "lint": "ruff check .", "typecheck": "mypy ."}
    return ProjectDetection(
        project_id="python", expected_name=expected_name, path=str(root), exists=True, language="Python",
        framework=framework, package_manager="pip" if (root / "requirements.txt").exists() else "pyproject",
        dependency_manifests=_relative(manifests, root), lockfiles=_relative([p for p in manifests if p.name.endswith(".lock")], root),
        commands=commands, important_files=_relative(manifests + sources[:30] + scripts[:10] + workflows[:10], root),
        test_strategy="pytest", deploy_strategy=deploy, integrations=[x for x in framework if x.startswith("AWS")],
        existing_agents_or_workflows=_relative(workflows, root),
        risks=["model-provider calls can consume tokens; use mocks in validation"],
        evidence=[f"detected imports/dependencies: {', '.join(framework)}", f"Python sources: {len(sources)}"],
    )


def discover_projects(settings: Settings) -> list[ProjectDetection]:
    return [
        detect_frontend(settings.frontend_path),
        detect_backend(settings.backend_path),
        detect_python(settings.python_path),
    ]


def discovery_markdown(projects: list[ProjectDetection], settings: Settings) -> str:
    lines = ["# Bezalel project discovery", "", f"Generated: {__import__('datetime').datetime.now().isoformat()}",
             "", "Expected aliases `bezalel-frontend`, `bezalel-backend`, and `bezalel-python` were resolved through configured paths and existing candidates.", ""]
    for p in projects:
        lines += [f"## {p.project_id}", f"- Expected name: `{p.expected_name}`", f"- Path: `{p.path}`", f"- Exists: `{p.exists}`",
                  f"- Language: `{p.language or 'unknown'}`", f"- Framework: {', '.join(p.framework) or 'none detected'}",
                  f"- Package/dependency manager: `{p.package_manager or 'unknown'}`", f"- Commands: `{p.commands}`",
                  f"- Tests: {p.test_strategy or 'not detected'}", f"- Deploy: {', '.join(p.deploy_strategy) or 'not_configured'}",
                  f"- Integrations: {', '.join(p.integrations) or 'none detected'}", f"- Important files: {', '.join(p.important_files[:15]) or 'none'}",
                  f"- Risks: {', '.join(p.risks) or 'none detected'}", ""]
    missing = [p for p in projects if not p.exists]
    if missing:
        lines += ["## Missing projects", *[f"- `{p.expected_name}` expected at `{p.path}`" for p in missing], ""]
    return "\n".join(lines)
