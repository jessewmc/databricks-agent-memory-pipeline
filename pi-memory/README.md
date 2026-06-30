# pi-memory

Wires **pi** (running hosted Claude) into the repo's dual-layer Lakebase memory.

```
pi (Claude) ──tools──► pi extension (TS) ──HTTP──► sidecar (FastAPI) ──► Lakebase
```

pi has no built-in MCP, so memory is exposed as pi custom tools (TypeScript) that
call a local Python **sidecar**. The sidecar reuses the repo's exact memory logic
(`memory_core.py`, ported from `agent-stateful-example/.../utils_memory.py`) against
the provisioned Lakebase instance + `databricks-gte-large-en` embeddings.

## Layout
- `sidecar/` — FastAPI service exposing the 9 memory tools + a preamble endpoint.
  - `memory_core.py` — framework-agnostic memory ops (no LangChain).
  - `server.py` — HTTP dispatch + long-lived `AsyncDatabricksStore`.
- `extension/` — pi TypeScript extension: starts/stops the sidecar, registers the
  9 memory tools, and injects the session-start memory snapshot into the system
  prompt. (Transcript writing on `session_shutdown` lands in Phase 5.)
- `.env.example` — config (instance name, schema, embedding, user id, port).
  Copy to `pi-memory/.env`; both the extension and the sidecar read it.

## Setup (recommended)
Run the setup script once from anywhere. It is idempotent: it symlinks the
extension into `.pi/extensions/` for auto-discovery, creates `pi-memory/.env`
from the example (if missing), and builds the sidecar venv.
```bash
./pi-memory/setup.sh
```
Then edit `pi-memory/.env`, log in (`databricks auth login --profile <profile>`),
and launch pi from the repo root (trust the project when prompted):
```bash
pi              # extension auto-loads; memory OFF by default
pi --memory     # extension auto-loads; memory ON from launch
```
Because the extension lives in an auto-discovered location, `/reload` hot-reloads
it after edits.

## Run with pi (manual / quick test)
```bash
cp .env.example .env            # then edit (instance, profile, user id)
# the extension spawns the sidecar; make its venv/deps available first:
cd sidecar && uv venv --python 3.12 && uv sync && cd ..

# load the extension without the symlink (quick test, no /reload):
pi -e pi-memory/extension/index.ts            # memory OFF (opt-in)
pi -e pi-memory/extension/index.ts --memory   # memory ON from launch
```

### Enabling / disabling memory (opt-in)
Memory is **off by default**. Turn it on either way:
- **At launch:** `pi --memory` (or `--memory=false` to force off).
- **In session:** `/memory on`, `/memory off`, `/memory status`.
- **Default via env:** `PI_MEMORY_ENABLED=1` makes the flag default on (the
  `--memory` flag still overrides per launch).

When off, the sidecar is not started, the 9 tools are deactivated (not offered to
the model, and they refuse if called directly), and no memory preamble is injected.
`/memory on` starts the sidecar, activates the tools, and captures the preamble
(injected on the next turn); `/memory off` stops the sidecar and deactivates them.

On enable the extension starts the sidecar, waits for `/health`, and caches the
memory preamble; on `session_shutdown` it stops the sidecar. The 9 tools forward
to the sidecar `/invoke`. Requires a valid Databricks login for the configured
profile (`databricks auth login --profile <profile>`) so the sidecar can reach
Lakebase + embeddings.

Extension env (read from `pi-memory/.env` or process env):
- `PI_MEMORY_ENABLED` — default state of the `--memory` flag (default off).
- `PI_MEMORY_PORT` — sidecar port (default 8765).
- `PI_MEMORY_USER_ID` / `PI_MEMORY_DEFAULT_USER_ID` — namespace-safe user id sent on
  each call (must match the transcript writer's `ai_chatbot.Chat.userId`).
- `PI_MEMORY_SIDECAR_CMD` / `PI_MEMORY_SIDECAR_ARGS` — override how the sidecar is
  spawned (default `uv run python server.py` in `sidecar/`).

## Run the sidecar standalone (for testing)
```bash
cd sidecar
uv venv --python 3.12
uv pip install fastapi "uvicorn[standard]" pydantic \
  "databricks-langchain[memory]>=0.20.0" "databricks-ai-bridge>=0.20.0" \
  "databricks-sdk>=0.79.0" python-dotenv
cp ../.env.example .env        # then edit
.venv/bin/python server.py
# health: curl localhost:8765/health
```

## HTTP API
- `GET  /health` → `{status, instance, schema}`
- `POST /invoke`  `{tool, user_id?, args}` → `{content}`
  - tools: `ls_memories`, `read_memory`, `write_memory`, `edit_memory`,
    `delete_memory`, `search_user_memories`, `ls_agent_memories`,
    `read_agent_memory`, `search_agent_memories`
- `POST /preamble` `{user_id?}` → `{text}` (memory map + always-loaded files)

## Notes
- `user_id` must be namespace-safe (no periods). The sidecar sanitizes defensively;
  keep it consistent with the transcript writer's `ai_chatbot.Chat.userId`.
- Agent-scoped tools are read-only by construction (no write endpoint for the
  `agent_memories` namespace) — the admin app curates that store.
- **Lakebase scale-to-zero (2h idle):** the provisioned instance is also exposed
  via the beta Postgres API as `projects/agent-memory-jesse-meade-clift` (same
  UID as the `database_instance`). Scale-to-zero lives on the compute *endpoint*,
  not the instance object, so it can't be set from the bundle's
  `database_instances` resource (databricks-bundles 1.4.0 has no field for it).
  It was set imperatively via CLI and must be re-applied if the instance is
  recreated:
  ```bash
  databricks postgres update-endpoint \
    projects/agent-memory-jesse-meade-clift/branches/production/endpoints/primary \
    spec.suspension \
    --json '{ "spec": { "suspend_timeout_duration": "7200s" } }' \
    --profile de_staging
  # disable again with:  --json '{ "spec": { "no_suspension": true } }'
  ```
  Valid range 60s–604800s. After 2h idle the endpoint auto-suspends and wakes on
  the next connection (the sidecar's first query incurs a cold-start wait).
