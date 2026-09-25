from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from orchestrator.config import Settings
from schemas.models import AgentResult


class AgentCLIAdapter(Protocol):
    """The shape every agent-execution backend must implement. CodexCLI
    (adapters/codex_cli.py) is the first implementation; a second real CLI
    (Claude Code) is a separate, later spec.
    """

    async def execute(self, prompt: str, workdir: Path, role: str, timeout: int | None = None,
                      cancel_event: Any = None, trace_metadata: dict[str, Any] | None = None,
                      on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                      reasoning_effort: str | None = None) -> AgentResult: ...

    async def execute_json(self, prompt: str, workdir: Path, schema: dict[str, Any], label: str,
                           timeout: int = 120, reasoning_effort: str | None = None) -> dict[str, Any] | None: ...


KNOWN_CLI_NAMES: set[str] = {"codex"}


def build_cli(name: str, settings: Settings) -> AgentCLIAdapter:
    if name == "codex":
        from adapters.codex_cli import CodexCLI
        return CodexCLI(settings)
    raise ValueError(f"unknown agent CLI '{name}' — must be one of {sorted(KNOWN_CLI_NAMES)}")


def validate_agent_roles(agent_roles: dict[str, dict[str, Any]], available_clis: set[str]) -> None:
    """Every role's primary "cli" must resolve to a real adapter the runtime
    was actually given — a typo must fail at startup, not at dispatch time.
    "fallback_cli" is deliberately NOT checked here: a role may name a
    fallback that doesn't exist yet (e.g. before a second real adapter is
    built), and that must not block startup.
    """
    for role, config in agent_roles.items():
        cli_name = config.get("cli", "codex")
        if cli_name not in available_clis:
            raise ValueError(
                f"role '{role}' declares cli='{cli_name}', which is not in the "
                f"configured clis {sorted(available_clis)}"
            )
