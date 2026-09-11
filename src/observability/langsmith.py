from __future__ import annotations

import re
import json
import os
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from orchestrator.config import Settings


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"(?i)(secret|token|password|api.?key|private.?key)", str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(sk-[A-Za-z0-9_-]+|AKIA[A-Z0-9]{12,})", "[REDACTED]", value)
        return re.sub(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", "[REDACTED PRIVATE KEY]", value, flags=re.S)
    return value


class LangSmithObserver:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None
        if settings.langsmith_tracing and settings.langsmith_api_key:
            try:
                from langsmith import Client
                self.client = Client(api_key=settings.langsmith_api_key, api_url=settings.langsmith_endpoint)
            except Exception:
                self.client = None

    @asynccontextmanager
    async def span(self, name: str, inputs: dict[str, Any], metadata: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
        run_id = None
        if self.client:
            try:
                run = self.client.create_run(name=name, run_type="chain", inputs=redact(inputs),
                                              project_name=self.settings.langsmith_project, extra={"metadata": redact(metadata or {})})
                run_id = getattr(run, "id", None) or (run.get("id") if isinstance(run, dict) else None)
            except Exception:
                run_id = None
        payload = {"name": name, "run_id": str(run_id) if run_id else None}
        try:
            yield payload
        except Exception as exc:
            if self.client and run_id:
                try:
                    self.client.update_run(run_id, error=str(exc))
                except Exception:
                    pass
            raise
        else:
            if self.client and run_id:
                try:
                    self.client.update_run(run_id, outputs={"status": "completed"})
                except Exception:
                    pass

    def event(self, execution_id: str, name: str, payload: dict[str, Any]) -> None:
        if not self.client:
            return
        try:
            run = self.client.create_run(name=name, run_type="tool", inputs=redact(payload),
                                         project_name=self.settings.langsmith_project,
                                         extra={"metadata": {"execution_id": execution_id}})
            run_id = getattr(run, "id", None) or (run.get("id") if isinstance(run, dict) else None)
            if run_id:
                self.client.update_run(run_id, outputs={"recorded": True})
        except Exception:
            return

    @contextmanager
    def codex_session(self, name: str, inputs: dict[str, Any], metadata: dict[str, Any]) -> Iterator[dict[str, str]]:
        """Create a LangSmith parent run and environment for the Codex tracing hook.

        The official Codex plugin reads the headers and reconstructs this run as
        its parent, preserving one tree for the dashboard execution and the
        detailed Codex transcript.
        """
        environment = dict(os.environ)
        safe_metadata = redact(metadata)
        if self.settings.langsmith_codex_tracing:
            environment["TRACE_TO_LANGSMITH"] = "true"
            environment["LANGSMITH_CODEX_PROJECT"] = self.settings.langsmith_codex_project
            environment["LANGSMITH_CODEX_METADATA"] = json.dumps(safe_metadata, default=str)
            if self.settings.langsmith_api_key:
                environment["LANGSMITH_CODEX_API_KEY"] = self.settings.langsmith_api_key
            if self.settings.langsmith_endpoint:
                environment["LANGSMITH_CODEX_ENDPOINT"] = self.settings.langsmith_endpoint

        run = None
        if self.client and self.settings.langsmith_codex_tracing:
            try:
                from langsmith.run_trees import RunTree

                run = RunTree(
                    name=name,
                    run_type="chain",
                    inputs=redact(inputs),
                    project_name=self.settings.langsmith_codex_project,
                    extra={"metadata": safe_metadata},
                    ls_client=self.client,
                )
                run.post()
                environment["LANGSMITH_CODEX_PARENT_HEADERS"] = json.dumps(run.to_headers())
            except Exception:
                run = None
        try:
            yield environment
        except Exception as exc:
            if run:
                try:
                    run.end(error=str(exc))
                    run.patch()
                except Exception:
                    pass
            raise
        else:
            if run:
                try:
                    run.end(outputs={"status": "completed"})
                    run.patch()
                except Exception:
                    pass
