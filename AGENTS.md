# Bezalel agent orchestrator — instructions for Codex

This repository hosts the orchestrator itself, not the Bezalel product code. The
`bezalel-app` (React), `Bezalel` (.NET 8) and `Workflow-IA` (Python/LangGraph)
projects are separate sibling repositories under active multi-agent governance.

## Default to the orchestrator for any work in the Bezalel projects

When the user asks you to implement, build, add, fix, review, analyze, or
audit something in `bezalel-app`, `Bezalel`, or `Workflow-IA` — call the MCP
tool `run_orchestrator_feature` (server `bezalel-orchestrator`) instead of
reading or editing those repositories yourself in this session.

That tool dispatches the request to the orchestrator's 7-agent LangGraph
pipeline (supervisor → frontend/backend/python_ai → contracts → qa → security →
reviewer → commit/merge/deploy gates), running in an isolated git worktree per
project. Working on those repos directly from this session skips every one of
those gates, and — for a review/analysis request — leaves no trace in the
Manage Agents panel for the user to see.

**Implement/build/fix a change** → `run_orchestrator_feature(feature_request)`
with `analysis_only` left `False`.

**Review/analyze/audit something (must not change anything)** →
`run_orchestrator_feature(feature_request, analysis_only=True)`. The real
agents still run and read the code and return real findings — the
orchestrator just refuses to commit, merge, or deploy in this mode, no matter
what the `AUTO_COMMIT`/`AUTO_MERGE`/`AUTO_DEPLOY` settings say. Do not do the
analysis yourself instead — a plain read-only investigation in this session
does not appear anywhere in the panel.

Requires the orchestrator API to be running (`bezalel-orchestrator api`, default
`http://127.0.0.1:8000`) — if the tool reports the API as unreachable, tell the
user to start it rather than falling back to editing the files yourself.

After dispatching, tell the user the execution id and that progress is visible
live in the Manage Agents panel (`/`) and `/events/view` — don't poll
`check_orchestrator_execution` in a tight loop; check it once if the user asks
for the outcome.

## Work on this repository itself

Changes to the orchestrator's own code (`src/`, `prompts/`, `tests/`,
`web/`) are normal Codex work in *this* session — the tool above is only for
requests aimed at the three managed projects, not for orchestrator development.
