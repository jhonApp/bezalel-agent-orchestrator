# Initial discovery record

This report records the repository audit performed before creating the orchestrator.

## Expected aliases and actual repositories

| Expected path | Actual path | Result |
|---|---|---|
| `../bezalel-frontend/` | `../bezalel-app/` | found; React/TypeScript/Vite/Tailwind/Vitest; npm and Bun lockfiles both exist |
| `../bezalel-backend/` | `../Bezalel/` | found; .NET 8/C#/ASP.NET/AWS/CDK; GitHub Actions CI/CD |
| `../bezalel-python/` | `../Workflow-IA/` | found; Python/LangGraph/AWS Lambda; pip requirements and deployment scripts |
| `../bezalel-agent-orchestrator/` | to be created | this project |

## Important existing configuration

- Backend: `CLAUDE.md`, `AGENTS.md`, `Bezalel.sln`, many `.csproj` files, `docker-compose.yml`, `.github/workflows/ci-cd.yml`, and CDK under `Bezalel.Infrastructure.Iac`.
- Frontend: `package.json`, `package-lock.json`, `bun.lockb`, `vite.config.ts`, `tailwind.config.ts`, `vercel.json`, and frontend/backend contract documentation under `docs/`.
- Python: `CLAUDE.md`, `requirements.txt`, `requirements-dev.txt`, `Dockerfile.build`, `deploy.ps1`, `deploy_topic_suggestions.ps1`, `make_zip.py`, LangGraph modules, and GitHub/workflow configuration.
- Codex CLI: `codex-cli 0.153.4`. PowerShell `codex` resolves to an unsigned `codex.ps1`; `codex.cmd` is the verified Windows executable and supports `codex exec --json --output-schema --output-last-message --cd --ephemeral --skip-git-repo-check`.

The runtime detector is authoritative after startup and rewrites `project-discovery-latest.md` without modifying business repositories.
