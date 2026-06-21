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
- `extension/` — pi TypeScript extension (Phase 4): starts/stops the sidecar,
  registers the tools, writes transcripts on `session_shutdown` (Phase 5).
- `.env.example` — config (instance name, schema, embedding, user id, port).

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
