from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during source-only bootstrap
    load_dotenv = None

from pydantic import BaseModel, Field


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings(BaseModel):
    workspace_root: Path
    orchestrator_root: Path
    frontend_path: Path
    backend_path: Path
    python_path: Path
    codex_command: str = "codex"
    langsmith_api_key: str | None = None
    langsmith_endpoint: str | None = None
    langsmith_project: str = "bezalel-agent-orchestrator"
    langsmith_tracing: bool = True
    langsmith_codex_tracing: bool = False
    langsmith_codex_project: str = "bezalel-agent-orchestrator"
    openai_api_key: str | None = None
    auto_commit: bool = True
    auto_merge: bool = True
    # Opt-in: pushes the agent's branch and opens a PR via `gh pr create` instead of merging
    # locally. When enabled it takes priority over auto_merge for the same run — merging
    # locally AND opening a PR for the same branch would be redundant. Merging main stays a
    # human decision made on the PR itself.
    create_pull_request: bool = False
    auto_deploy: bool = True
    allow_production_deploy: bool = False
    deploy_environment: str = "development"
    checkpoint_backend: str = "sqlite"
    checkpoint_sqlite_path: Path = Path(".data/checkpoints.sqlite3")
    langgraph_checkpoint_sqlite_path: Path = Path(".data/langgraph-checkpoints.sqlite3")
    agent_timeout_seconds: int = 1800
    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    use_worktrees: bool = True
    cleanup_worktrees: bool = False
    codex_approval_policy: str = "never"
    codex_sandbox: str = "workspace-write"
    # contracts/qa/security/reviewer (and the domain classifier) are judgment calls over an
    # already-produced diff, not code generation — lower reasoning effort cuts token/latency
    # cost for them specifically. frontend/backend/python_ai keep the CLI's own default (None
    # here means "do not pass -c model_reasoning_effort", i.e. no override).
    codex_reasoning_effort_gates: str | None = "low"
    context_token_warning: int = 24000
    context_token_critical: int = 60000
    cost_per_1m_input: float = 1.75
    cost_per_1m_output: float = 14.0
    max_output_chars: int = 12000
    # A single Codex --json line can legitimately be large (a tool result embedding several
    # files' content); asyncio's default StreamReader limit (64 KiB) is well below that and
    # raises LimitOverrunError rather than truncating, which the adapter recovers from either
    # way — this just makes hitting the limit rare in normal use.
    codex_stream_limit: int = 10 * 1024 * 1024
    project_id: str = "bezalel"
    agent_platform_design_root: Path | None = None

    @property
    def checkpoint_path(self) -> Path:
        path = self.checkpoint_sqlite_path
        return path if path.is_absolute() else self.orchestrator_root / path

    @property
    def langgraph_checkpoint_path(self) -> Path:
        path = self.langgraph_checkpoint_sqlite_path
        return path if path.is_absolute() else self.orchestrator_root / path

    @classmethod
    def load(cls, cwd: Path | None = None) -> "Settings":
        cwd = (cwd or Path.cwd()).resolve()
        env_file = cwd / ".env"
        if load_dotenv:
            load_dotenv(env_file, override=False)

        configured_root = os.getenv("WORKSPACE_ROOT", "").strip()
        if configured_root:
            workspace = Path(configured_root).expanduser().resolve()
        else:
            workspace = cwd.parent if cwd.name == "bezalel-agent-orchestrator" else cwd.parent

        orchestrator_root = cwd if cwd.name == "bezalel-agent-orchestrator" else workspace / "bezalel-agent-orchestrator"

        def choose(env_name: str, candidates: list[str]) -> Path:
            configured = os.getenv(env_name, "").strip()
            if configured:
                return Path(configured).expanduser().resolve()
            for candidate in candidates:
                path = workspace / candidate
                if path.exists():
                    return path.resolve()
            return (workspace / candidates[0]).resolve()

        return cls(
            workspace_root=workspace,
            orchestrator_root=orchestrator_root,
            frontend_path=choose("FRONTEND_PATH", ["bezalel-frontend", "bezalel-app"]),
            backend_path=choose("BACKEND_PATH", ["bezalel-backend", "Bezalel"]),
            python_path=choose("PYTHON_PATH", ["bezalel-python", "Workflow-IA"]),
            codex_command=os.getenv("CODEX_COMMAND", "codex"),
            langsmith_api_key=os.getenv("LANGSMITH_CODEX_API_KEY") or os.getenv("LANGSMITH_API_KEY") or None,
            langsmith_endpoint=os.getenv("LANGSMITH_CODEX_ENDPOINT") or os.getenv("LANGSMITH_ENDPOINT") or None,
            langsmith_project=os.getenv("LANGSMITH_PROJECT", "bezalel-agent-orchestrator"),
            langsmith_tracing=_bool("LANGSMITH_TRACING", True),
            langsmith_codex_tracing=_bool("TRACE_TO_LANGSMITH", False),
            langsmith_codex_project=os.getenv("LANGSMITH_CODEX_PROJECT") or os.getenv("LANGSMITH_PROJECT", "bezalel-agent-orchestrator"),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            auto_commit=_bool("AUTO_COMMIT", True),
            auto_merge=_bool("AUTO_MERGE", True),
            create_pull_request=_bool("CREATE_PULL_REQUEST", False),
            auto_deploy=_bool("AUTO_DEPLOY", True),
            allow_production_deploy=_bool("ALLOW_PRODUCTION_DEPLOY", False),
            deploy_environment=os.getenv("DEPLOY_ENVIRONMENT", "development"),
            checkpoint_backend=os.getenv("CHECKPOINT_BACKEND", "sqlite"),
            checkpoint_sqlite_path=Path(os.getenv("CHECKPOINT_SQLITE_PATH", ".data/checkpoints.sqlite3")),
            langgraph_checkpoint_sqlite_path=Path(os.getenv("LANGGRAPH_CHECKPOINT_SQLITE_PATH", ".data/langgraph-checkpoints.sqlite3")),
            agent_timeout_seconds=_int("AGENT_TIMEOUT_SECONDS", 1800),
            max_retries=_int("MAX_RETRIES", 2),
            retry_backoff_seconds=_float("RETRY_BACKOFF_SECONDS", 2.0),
            use_worktrees=_bool("USE_WORKTREES", True),
            cleanup_worktrees=_bool("CLEANUP_WORKTREES", False),
            codex_approval_policy=os.getenv("CODEX_APPROVAL_POLICY", "never"),
            # workspace-write rejects even read-only commands (git status, ls) under this Codex
            # CLI's Windows sandbox implementation — danger-full-access is what the working
            # --dangerously-bypass-approvals-and-sandbox interactive sessions already use;
            # git worktree isolation is the real containment, not the OS sandbox, on this platform.
            codex_sandbox=os.getenv("CODEX_SANDBOX", "danger-full-access" if os.name == "nt" else "workspace-write"),
            codex_reasoning_effort_gates=os.getenv("CODEX_REASONING_EFFORT_GATES", "low").strip() or None,
            context_token_warning=_int("CONTEXT_TOKEN_WARNING", 24000),
            context_token_critical=_int("CONTEXT_TOKEN_CRITICAL", 60000),
            cost_per_1m_input=_float("CONTEXT_COST_PER_1M_INPUT", 1.75),
            cost_per_1m_output=_float("CONTEXT_COST_PER_1M_OUTPUT", 14.0),
            agent_platform_design_root=(
                Path(os.getenv("AGENT_PLATFORM_DESIGN_ROOT")).expanduser().resolve()
                if os.getenv("AGENT_PLATFORM_DESIGN_ROOT")
                else (workspace.parent / "projeto-agents-plataform").resolve()
            ),
        )
