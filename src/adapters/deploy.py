from __future__ import annotations

import json
from pathlib import Path

from orchestrator.config import Settings
from schemas.models import CommandResult, DeployResult, ProjectDetection

from .command import run_command


class DeployAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def detect(self, project: ProjectDetection) -> dict[str, str | None]:
        root = Path(project.path)
        evidence = "\n".join(str(p) for p in project.important_files + project.existing_agents_or_workflows)
        if "vercel.json" in evidence and "deploy" in project.commands:
            return {"strategy": "vercel", "command": project.commands["deploy"], "evidence": "vercel.json + deploy script"}
        if (root / "cdk.json").exists() or "AWS CDK" in project.deploy_strategy:
            return {"strategy": "cdk", "command": None, "evidence": "cdk.json or CDK project; requires explicit target"}
        if (root / "template.yaml").exists() or (root / "template.yml").exists():
            return {"strategy": "sam", "command": "sam deploy", "evidence": "SAM template"}
        if (root / "serverless.yml").exists() or (root / "serverless.yaml").exists():
            return {"strategy": "serverless", "command": "serverless deploy", "evidence": "serverless config"}
        if any(Path(project.path).glob("Dockerfile*")):
            return {"strategy": "docker", "command": None, "evidence": "Dockerfile; registry/target not declared"}
        if "GitHub Actions" in project.deploy_strategy:
            return {"strategy": "github-actions", "command": None, "evidence": "workflow detected; local dispatch not inferred"}
        return {"strategy": None, "command": None, "evidence": None}

    async def execute(self, project: ProjectDetection, cwd: Path | None = None) -> DeployResult:
        target = Path(cwd or project.path)
        if self.settings.deploy_environment.lower() in {"prod", "production"} and not self.settings.allow_production_deploy:
            return DeployResult(project_id=project.project_id, status="blocked", reason="production deploy disabled by ALLOW_PRODUCTION_DEPLOY")
        detected = self.detect(project)
        strategy, command = detected["strategy"], detected["command"]
        if not strategy:
            return DeployResult(project_id=project.project_id, status="not_configured", reason="no recognized deploy strategy")
        if not command:
            return DeployResult(project_id=project.project_id, strategy=strategy, status="not_configured",
                                reason="strategy detected but no safe local command was explicitly configured")
        if not self.settings.auto_deploy:
            return DeployResult(project_id=project.project_id, strategy=strategy, status="skipped", reason="AUTO_DEPLOY=false")
        parts = command.split()
        result = await run_command(parts, target, timeout=self.settings.agent_timeout_seconds)
        return DeployResult(project_id=project.project_id, strategy=strategy,
                            status="deployed" if result.ok else "failed", command=parts,
                            output=(result.stdout + "\n" + result.stderr)[-12000:], duration_seconds=result.duration_seconds,
                            reason=None if result.ok else "deploy command failed")
