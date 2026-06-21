# Plan: Local pi memory testbed (Architecture B)

Goal: run **pi (hosted Claude) as the agent**, give it the repo's dual-layer Lakebase
memory, persist pi's transcripts so the **dreamer** jobs can distill them, and keep the
**agent-database-admin** app for curating shared memory. A **Python Databricks Asset
Bundle (DAB)** provisions the Databricks-side resources. Stateless example, Genie, and
you-com are out of scope.

## Architecture

```
                 ┌─────────────────────────────────────────────┐
   you ──► pi ──►│ hosted Claude (pi's provider)                │
                 │  + memory tools (registered by pi extension) │
                 └───────────────┬─────────────────────────────┘
                                 │ HTTP (localhost)
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │ Python sidecar (FastAPI)                     │
                 │  reuses utils_memory tool logic              │
                 │  AsyncDatabricksStore (user + agent ns)      │
                 └───────────────┬─────────────────────────────┘
                                 │ psql + embeddings
                                 ▼
   ┌──────────────────────── Databricks ─────────────────────────┐
   │ Lakebase (PROVISIONED instance)   databricks-gte-large-en     │
   │   schema `memories`: store / store_vectors / checkpoints      │
   │   namespaces: user_memories/<uid>, agent_memories             │
   │   schema `ai_chatbot`: Chat / Message  ◄── pi transcripts     │
   │ MLflow experiment   UC volume dreamer_logs   2 dreamer jobs   │
   │ agent-database-admin app (curates agent_memories)             │
   └──────────────────────────────────────────────────────────────┘
```

Why a sidecar: pi has no built-in MCP and registers tools in TypeScript, but the memory
backend (`AsyncDatabricksStore`, `databricks_ai_bridge`) is Python. A long-lived local
Python service lets us reuse the exact tool logic in `utils_memory.py` instead of
reimplementing vector search / embeddings / connection pooling in Node. The pi docs
explicitly bless this (`session_start` to start, `session_shutdown` to stop).

## Key design decisions (locked)

1. Scope: stateful memory + admin app + 2 dreamer jobs. No stateless, Genie, you-com.
2. Lakebase: **provisioned** single instance (testing). Both stores live in it,
   separated by namespace (`user_memories/<uid>` vs `agent_memories`) in schema
   `memories` — functionally identical to the two-project prod layout for our purposes.
3. Model: hosted Claude via pi. No LLM endpoint to provision.
4. Embeddings: keep `databricks-gte-large-en` (requires Databricks auth locally).
5. Dreamer distillation LLM: stays `ChatDatabricks` (jobs run on Databricks; native auth,
   no key management). Only fix the endpoint name to a currently-valid one.
6. Transcripts: pi writes finished sessions into `ai_chatbot.Chat`/`Message` (the exact
   tables the dreamer already reads), so the dreamer needs only the branch-walk removed.

## Locked decisions

- Workspace: DE `staging_dev` (`dbc-8ce48177-2476`, profile `de_staging`).
- Catalog `de_staging`, schema `agent_memory_demo` (dev mode → `dev_jesse_meadeclift_agent_memory_demo`),
  volume `dreamer_logs`.
- Provisioned Lakebase: native DAB `database_instances` resource (CLI v1.1.0 supports it;
  no provisioning job needed). Name `agent-memory-jesse-meade-clift`, capacity `CU_1`.
- `user_id` for pi sessions: Databricks username `jesse.meade-clift@clio.com`.
- User-scoping: `staging_dev` uses `mode: development` (auto prefix + dev/owner/project
  tags). database_instance is NOT auto-prefixed by dev mode, so it is manually scoped via
  the `owner_slug` var + `custom_tags`.

Note: `databricks-bundles` 1.4.0 has typed Python builders only for jobs/pipelines/
schemas/volumes/catalogs. experiment, app, and database_instance are declared in
`resources/infra.yml` (YAML) and merged with the Python resources → **hybrid bundle**.

## Status

- [x] **Phase 1 — DAB skeleton** (`bundle/`): schema + volume (Python) and
      database_instance + experiment (YAML). `databricks bundle validate -t staging_dev`
      passes; resolved names confirmed user-scoped.
- [ ] Phase 2 — provision Lakebase + schema + grants
- [ ] Phase 3 — Python sidecar
- [ ] Phase 4 — pi extension
- [ ] Phase 5 — transcript writer
- [ ] Phase 6 — dreamer jobs
- [ ] Phase 7 — admin app

## Components to build

### 1. `bundle/` — Python DAB
```
bundle/
  databricks.yml          # thin loader: experimental.python + targets (staging_dev/staging)
  pyproject.toml          # databricks-bundles
  resources/
    __init__.py
    memory_pipeline.py     # load_resources(bundle) -> Resources
  provision/
    provision.py           # entrypoint job: Lakebase instance (if not DAB-native),
                           #   schemas, grants — reuses setup_memories_schema + grants logic
```
DAB-managed: MLflow experiment, UC schema, `dreamer_logs` volume, 2 dreamer jobs, admin app,
and (if supported) the provisioned Lakebase instance. Imperative job handles schema creation
+ Postgres role/grants for the admin-app SP, reusing existing scripts.

### 2. `pi-memory/` — pi integration (new top-level dir)
```
pi-memory/
  sidecar/
    pyproject.toml          # fastapi, uvicorn, databricks-langchain[memory], databricks-ai-bridge
    server.py               # FastAPI wrapping the 9 memory tools against provisioned Lakebase
    memory_core.py          # extracted, framework-agnostic memory ops (refactor of utils_memory)
  extension/
    index.ts                # pi extension: starts sidecar on session_start, registers 9 tools,
                            #   writes transcript to Lakebase on session_shutdown, stops sidecar
    package.json
  .env.example              # LAKEBASE_INSTANCE_NAME, DATABRICKS_*, PI_MEMORY_USER_ID, port
  README.md
```
- The 9 tools mirror `memory_tools()`: `ls_memories`, `read_memory`, `write_memory`,
  `edit_memory`, `delete_memory`, `search_user_memories`, `ls_agent_memories`,
  `read_agent_memory`, `search_agent_memories`.
- Optional: a `before_agent_start` hook injects the `build_memory_preamble` snapshot
  (memory map + always-loaded files) into the system prompt, matching the real agent.
- Transcript writer: on `session_shutdown`, read pi session entries, POST to the sidecar,
  which inserts into `ai_chatbot.Chat`/`Message` with `userId`, `chatId` (= pi session id),
  `role`, `parts` (JSON), `createdAt`.

### 3. Dreamer notebook edits (`.py` conversions per repo convention)
- Replace child-branch discovery with a single provisioned-instance connection.
- Point `LakebaseClient` / `AsyncDatabricksStore` at the instance (not project/branch).
- Set `REPORT_DIR` / `LOG_DIR` to the real `/Volumes/<catalog>/<schema>/dreamer_logs`.
- Fix LLM endpoint name (`databricks-claude-opus-4-7` is likely invalid) to a valid one.
- Keep `ai_chatbot.Message`/`Chat` read query as-is.

### 4. agent-database-admin
- Repoint its Lakebase resource from autoscaling project/branch to the provisioned
  instance; run `setup_memories_schema.py` (adapted) + grants for its SP.
- Otherwise deploy as-is via the bundle `apps` resource.

## Build phases

1. **DAB skeleton** — `bundle/` with experiment + schema + volume; `databricks bundle validate`.
2. **Lakebase provisioning** — instance (native or via job) + schema + grants; verify
   `AsyncDatabricksStore(instance_name=...).setup()` creates tables locally.
3. **Sidecar** — extract `memory_core.py`, build FastAPI, test the 9 ops against Lakebase.
4. **pi extension** — start/stop sidecar, register tools, manual chat test in pi.
5. **Transcript writer** — `session_shutdown` → `ai_chatbot` tables; verify rows land.
6. **Dreamer** — convert + repoint notebooks; add as scheduled jobs; run once on real data.
7. **Admin app** — repoint + deploy; curate an `agent_memories` file; confirm pi reads it.

## Risks / notes
- Local sidecar needs Databricks auth (OAuth profile) for embeddings + Lakebase psql.
- Provisioned Lakebase has no branches; the prod per-session-branch isolation is dropped
  for testing (single shared DB). Acceptable and simpler; note when moving toward prod.
- Embedding dims must stay 1024 (`databricks-gte-large-en`) everywhere or vectors break.
```
