from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from orchestrator.config import Settings
from schemas.models import ContextHealth


def estimate_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return max(0, len(text) // 4)


class ContextHealthMonitor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def assess(self, state: dict[str, Any]) -> ContextHealth:
        tokens = estimate_tokens({k: state.get(k) for k in ("feature_request", "architecture_summary", "plan", "contracts", "errors", "final_report", "messages", "tool_outputs")})
        files = sum(len(str(item)) for item in state.get("files_changed", []))
        errors = state.get("errors", [])
        retries = int(state.get("retries", 0))
        previous = state.get("context_health", {}) or {}
        reasons: list[str] = []
        if tokens >= self.settings.context_token_critical:
            level = "critical"
            reasons.append("estimated context tokens exceed critical threshold")
        elif tokens >= self.settings.context_token_warning:
            level = "warning"
            reasons.append("estimated context tokens exceed warning threshold")
        else:
            level = "healthy"
        if len(errors) >= 3:
            level = "critical"
            reasons.append("three or more errors recorded")
        if retries >= self.settings.max_retries:
            reasons.append("retry budget is exhausted")
        repeated = self._repeated_values(state)
        if repeated >= 5:
            reasons.append("repeated information detected")
        health = ContextHealth(
            level=level, message_count=int(state.get("message_count", 0)), estimated_tokens=tokens,
            file_chars=files, tool_calls=int(state.get("tool_calls", 0)), repeated_information=repeated,
            stale_information=int(state.get("stale_information", 0)),
            contract_conflicts=sum(1 for c in state.get("contracts", []) if c.get("severity") == "blocking"),
            retries=retries, consecutive_errors=int(state.get("consecutive_errors", 0)),
            estimated_cost=float(state.get("estimated_cost", 0.0)),
            duration_seconds=float(state.get("latency", {}).get("total", 0.0)),
            compacted=bool(previous.get("compacted", False)),
            summary_id=previous.get("summary_id"), reasons=reasons,
        )
        return health

    @staticmethod
    def _repeated_values(state: dict[str, Any]) -> int:
        values = []
        for key in ("errors", "files_changed", "contracts"):
            for value in state.get(key, []):
                values.append(hashlib.sha1(str(value).encode()).hexdigest())
        return sum(count - 1 for count in Counter(values).values() if count > 1)

    def compact(self, state: dict[str, Any]) -> dict[str, Any]:
        health = self.assess(state)
        if health.level == "healthy":
            return state
        summary = {
            "execution_id": state.get("execution_id"), "feature_request": state.get("feature_request"),
            "architecture_summary": state.get("architecture_summary"), "status": state.get("status"),
            "next_action": state.get("next_action"), "acceptance": [t.get("acceptance_criteria", []) for t in state.get("plan", [])],
            "contracts": state.get("contracts", []), "errors": state.get("errors", [])[-10:],
            "files_changed": state.get("files_changed", [])[-100:], "decisions": state.get("decisions", [])[-20:],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        summary_id = hashlib.sha1(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()[:16]
        state = dict(state)
        state["context_summary"] = summary
        state["context_compacted"] = True
        state["context_health"] = {**health.model_dump(mode="json"), "compacted": True, "summary_id": summary_id}
        state["message_count"] = max(1, int(state.get("message_count", 0)) // 2)
        state["architecture_summary"] = summary["architecture_summary"] or "context compacted; see context_summary"
        return state
