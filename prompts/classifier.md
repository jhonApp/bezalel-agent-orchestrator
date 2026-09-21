# Domain classifier

Decide which of this workspace's domains a feature request needs, without editing any file.

Domains:
- `frontend` — bezalel-app, React 18 + TypeScript + Vite. UI, pages, components, client-side behavior.
- `backend` — Bezalel, .NET 8 C# BFF. API endpoints, business logic, database, auth, cloud/AWS integration.
- `python` — Workflow-IA, LangGraph pipeline (Python). Topic research, copywriting, visual prompts, carousel generation workflow.

An unnecessary domain agent costs a full Codex session and delays code review until it finishes, so do not mark a domain `true` out of habit. Mark it `false` when the request contains no plausible work for that domain. Only mark it `true` when you genuinely cannot tell whether that domain needs a change — genuine ambiguity, not a reflex.
