# Wake Listener setup (on-premise, on-demand exposure)

Exposes the real orchestrator (this machine) to the internet through ngrok's
free static domain, without keeping the heavy orchestrator process running
24/7. See `docs/superpowers/specs/2026-09-22-on-premise-wake-listener-design.md`
for the design rationale.

## 1. Environment variables

Set these before starting the listener. Unlike the orchestrator API itself
(`Settings.load()` in `orchestrator/config.py`, which reads a `.env` file via
`python-dotenv`), the wake listener does not load `.env` — export these
directly in the shell or service environment instead:

| Variable | Default | Purpose |
|---|---|---|
| `WAKE_LISTENER_HOST` | `127.0.0.1` | interface the listener binds on |
| `WAKE_LISTENER_PORT` | `8090` | port the listener binds on (point ngrok here) |
| `WAKE_BACKEND_HOST` | `127.0.0.1` | where the real orchestrator API listens |
| `WAKE_BACKEND_PORT` | `8000` | must match `bezalel-orchestrator api`'s `--port` |
| `WAKE_LISTENER_TOKEN` | *(none — auth disabled)* | shared secret; **set this before exposing publicly** |
| `WAKE_IDLE_TIMEOUT_MINUTES` | `20` | how long with no activity before the backend is stopped |
| `WAKE_IDLE_CHECK_INTERVAL_SECONDS` | `60` | how often the idle check runs |
| `WAKE_CORS_ALLOWED_ORIGINS` | `https://vercel-deploy-orchestrator.vercel.app` | comma-separated list of dashboard origins allowed to call this listener |
| `WAKE_BACKEND_HEALTH_PATH` | `/dashboard-data` | path polled to decide whether the backend is awake |
| `WAKE_BACKEND_HEALTH_TIMEOUT_SECONDS` | `30` | how long to wait for the backend to become healthy after spawning it |
| `WAKE_BACKEND_START_COMMAND` | `bezalel-orchestrator,api` | comma-separated command used to spawn the backend |
| `WAKE_BACKEND_CWD` | the repository root | working directory the spawned backend runs from — critical if you run the listener from a service/scheduler whose default cwd is not the repo root |

## 2. Run the listener

```bash
bezalel-wake-listener
```

Leave this running (Windows: Task Scheduler entry "at log on", or a small
wrapper service) — this is the one process meant to run 24/7. It does not run
Codex or touch git; it only spawns/stops `bezalel-orchestrator api` on demand.

## 3. Install ngrok and reserve a static domain

1. Install ngrok: https://ngrok.com/download
2. `ngrok config add-authtoken <your-authtoken>` (from the ngrok dashboard)
3. Reserve a free static domain from the ngrok dashboard (Domains -> New Domain)
4. Install as a Windows service pointed at the listener's port:
   ```
   ngrok service install --config <path-to-ngrok.yml>
   ```
   where `ngrok.yml` contains:
   ```yaml
   version: 3
   agent:
     authtoken: <your-authtoken>
   tunnels:
     wake-listener:
       proto: http
       addr: 8090
       domain: <your-static-domain>.ngrok-free.app
   ```
5. `ngrok service start`

## 4. Point the dashboard at it

Open `https://vercel-deploy-orchestrator.vercel.app/` — on first load (from a
non-localhost origin) it prompts once for the listener URL
(`https://<your-static-domain>.ngrok-free.app`) and the token, then stores
both in `localStorage`. See Task 5 of
`docs/superpowers/plans/2026-09-22-wake-listener.md` for what changed in the
dashboard itself.

## 5. Verify

```bash
curl https://<your-static-domain>.ngrok-free.app/ping
```

Expected: `{"status":"wake_listener_ok"}` — this alone does not require the
token and does not wake the backend. Then, with the token:

```bash
curl -H "Authorization: Bearer <token>" https://<your-static-domain>.ngrok-free.app/dashboard-data
```

The first call after a period of inactivity takes a few seconds (the listener
is spawning and health-checking the real backend); subsequent calls are fast
until the idle timeout elapses again.

**Note on `/events` (SSE):** the browser's `EventSource` API cannot set custom
headers, so the dashboard sends the token as a `?token=<token>` query
parameter for that one path instead of a header — this is a narrow, deliberate
exception: every other path still requires the `Authorization: Bearer <token>`
header and rejects a query-string token.
