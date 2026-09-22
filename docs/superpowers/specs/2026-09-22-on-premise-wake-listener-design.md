# On-Premise Wake Listener — Design

## Problem

The Vercel deployment (`https://vercel-deploy-orchestrator.vercel.app/`) cannot run
real orchestrator executions. Reproduced live: `POST /executions` (dry_run) returned
`202` with an execution id; the immediately-following `GET /executions/{id}` returned
`404 execution not found`. Root cause, confirmed by reading `index.py` and
`src/api/app.py`:

- Vercel's Python runtime freezes the process once the HTTP response is sent —
  `asyncio.create_task(graph.run(...))` never gets to finish.
- `CHECKPOINT_SQLITE_PATH` is forced to `/tmp/...` — ephemeral per instance; a POST
  and the GET that follows it are not guaranteed to land on the same instance.
- The `codex` CLI binary does not exist in the Vercel Python function image.
- The three sibling repos (`bezalel-app/`, `Bezalel/`, `Workflow-IA/`) that agents
  write into via git worktrees are not part of the Vercel deployment — only this
  repo is.

This is an architecture mismatch, not a bug: the orchestrator needs a persistent
process, a real filesystem, `git`, and the `codex` binary — none of which a
serverless function provides.

## Goal

Keep the Vercel-hosted page as the dashboard UI, but have it talk to the real
orchestrator running on the user's own Windows machine (where everything above
already works), reachable at a stable public URL, while that machine stays idle
(no heavy process running) until an actual request comes in — "on-premise lambda."

## Chosen approach

A small always-on **Wake Listener** process in front of the real orchestrator API,
exposed via an **ngrok** static free domain (no owned domain required, confirmed
2026: ngrok's free plan includes one persistent static domain,
https://ngrok.com/blog/free-static-domains-ngrok-users). Considered and rejected:

- **Cloudflare Tunnel** — free and built for 24/7 production traffic, but requires
  a domain the user controls in Cloudflare DNS; the user does not have one (the
  domain they first pointed at, `vercel-deploy-orchestrator.vercel.app`, is
  Vercel's own, not theirs).
- **Tailscale Funnel** — free, no domain needed, but has an open 2026 reliability
  issue for background/always-on config (`--bg` config not synced to the control
  plane until daemon restart — tailscale/tailscale#19508) and Tailscale's own docs
  frame Funnel as meant for short-lived exposure, not a permanent endpoint.
- **Always-on orchestrator with no wake/idle logic** — simplest, but explicitly
  not what was asked: the machine should stay idle until triggered, not run the
  heavy process 24/7.

## Architecture

```
Internet (Vercel dashboard JS, MCP callers)
   -> ngrok static domain (https://<name>.ngrok-free.app)
   -> Wake Listener (new, tiny, always running, e.g. port 8090)
        - validates a shared-secret bearer token
        - if the orchestrator API (port 8000) is not up: spawns it, waits for health
        - reverse-proxies the request through (SSE included)
        - tracks activity (including open SSE streams) to drive an idle timer
        - after N idle minutes with no open streams: stops the orchestrator process
   -> Orchestrator API (existing FastAPI app, port 8000) - only running while "awake"
```

The ngrok agent runs as a Windows service (`ngrok service install`), always
connected; its ~8h session cycling is handled by the agent's own auto-reconnect
and the static domain reassigns automatically, so it should be transparent to
callers.

## Components

1. **Wake Listener** (new — `src/wake_listener/` or a standalone script, stdlib +
   `httpx`/`starlette`, no heavy dependencies since this is the one process that
   truly runs 24/7):
   - Transparent reverse proxy for every path (`/`, `/dashboard-data`,
     `/executions*`, `/events*`, `/mcp*`, ...) — the dashboard's existing
     relative-path `fetch`/`EventSource` calls do not need to change shape, only
     their base URL.
   - Bearer-token check before any spawn or proxy attempt, on every path except a
     public `/ping` health check. Wrong/missing token -> `401`, no spawn — a
     stranger hitting the tunnel must not be able to trigger a Codex session
     against the user's paid plan.
   - Spawn: `subprocess.Popen` for `bezalel-orchestrator api`, detached, with its
     PID recorded by the listener itself.
   - Health wait: poll `GET http://127.0.0.1:8000/dashboard-data` until `200` or a
     30s timeout; `503` with a clear message if the backend never comes up, rather
     than proxying into a broken process.
   - Idle timer: last-activity timestamp plus an open-SSE-stream counter. A
     background loop checks once a minute; stops the orchestrator only when both
     "idle longer than `IDLE_TIMEOUT_MINUTES`" (default 20, env-configurable) AND
     "zero open streams" are true — an open `/events` connection must never be
     killed out from under a live dashboard.
   - Shutdown: terminate only the PID this listener itself spawned (matches this
     project's existing incident lesson about never using a broad process-name
     kill) — `taskkill /PID <pid> /T` or `process.terminate()`, not a name-based
     kill.

2. **ngrok** — Windows service, one reserved free static domain, forwards to the
   Wake Listener's port (not directly to 8000 — the listener must see every
   request to do the spawn/idle logic).

3. **Dashboard (`web/Plataforma de Administração.dc.html`)** — one change: the
   base URL for `fetch`/`EventSource` calls becomes the ngrok domain instead of a
   relative path (different origin than the Vercel-served HTML, so the Wake
   Listener's proxy responses need CORS headers for the Vercel origin). Vercel
   keeps serving only the static HTML/JS/CSS — it is not asked to run any
   orchestrator logic, sidestepping the entire class of problem in "Problem"
   above.

4. **Token distribution** — the shared secret is never committed into the
   dashboard's HTML (that file is served publicly from Vercel). On first load,
   if no token is found in `localStorage`, the dashboard prompts once for it and
   stores it there; every subsequent call reads it from `localStorage` and sends
   it as `Authorization: Bearer <token>`. Rotating the token means clearing
   `localStorage` and re-entering it — acceptable for a single-operator tool.

## Data flow (one request)

1. Browser loads the dashboard (static) from `vercel-deploy-orchestrator.vercel.app`.
2. Dashboard JS reads the token from `localStorage` (prompts once if absent).
3. Dashboard calls `https://<name>.ngrok-free.app/dashboard-data` with the bearer
   token.
4. ngrok forwards to the Wake Listener.
5. Listener validates the token (401 and stop here if invalid).
6. Listener checks whether the orchestrator API is already up; if not, spawns it
   and waits for health.
7. Listener proxies the request through, streaming the response back unmodified.
8. Listener records the activity timestamp (and, for `/events`, holds the stream
   open in its counter until the client disconnects).
9. The idle loop stops the orchestrator process once both idle conditions above
   are met.

## Error handling

- Missing/invalid token -> `401`, no process spawned.
- Backend fails to become healthy within 30s -> `503` with the actual reason (the
  listener's own health-poll failure), not a generic proxy error.
- Idle-stop targets only the PID the listener itself recorded — never a
  process-name-based kill.
- An open SSE connection always counts as activity for the idle timer, for its
  entire duration, not just at connect time.
- ngrok's own session-cycling (~8h) is the agent's concern, not the listener's —
  the listener only binds a local port and never talks to ngrok directly.

## Testing

- Unit tests for the Wake Listener's own logic — spawn-if-absent, idle-timeout
  math (including the open-stream guard), and token validation — against a fake
  subprocess and a fake backend bound to a random local port. No real ngrok or
  Codex CLI needed for these.
- One manual end-to-end check with the real ngrok tunnel and a real orchestrator
  spawn, once the listener is implemented, to confirm the whole chain actually
  works before calling this done.
- The existing 154-test orchestrator suite is untouched — the Wake Listener is a
  new process layered in front, not a modification to `nodes.py`/`graph.py`/the
  FastAPI app itself.

## Out of scope

- Any change to how Vercel's own Python function behaves — it is demoted to a
  pure static host for the dashboard's HTML/JS, nothing more.
- Multi-user auth (OAuth, per-user roles) — one shared bearer token is enough for
  a single-operator tool.
- High availability / multiple orchestrator instances — this is deliberately one
  always-idle machine, woken on demand, per the user's explicit ask.
