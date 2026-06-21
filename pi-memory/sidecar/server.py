"""FastAPI sidecar exposing the Lakebase memory tools to the pi extension.

Holds one long-lived AsyncDatabricksStore (both user and agent namespaces live in
the same provisioned instance/schema, separated by namespace prefix). The pi
extension starts this on session_start and stops it on session_shutdown.

Run:  uv run sidecar           (reads .env / process env)
Env:
  LAKEBASE_INSTANCE_NAME        provisioned Lakebase instance (required)
  LAKEBASE_AGENT_MEMORY_SCHEMA  Postgres schema for the store (default: memories)
  DATABRICKS_EMBEDDING_ENDPOINT embedding endpoint   (default: databricks-gte-large-en)
  PI_MEMORY_DEFAULT_USER_ID     fallback namespace-safe user id (default: pi_user)
  PI_MEMORY_PORT                listen port          (default: 8765)
  DATABRICKS_CONFIG_PROFILE     auth profile for Databricks (e.g. de_staging)
"""

import os
import re
from contextlib import asynccontextmanager
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()  # standalone runs; the pi extension passes env directly when spawning

import uvicorn  # noqa: E402
from databricks_langchain import AsyncDatabricksStore
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import memory_core as mc

INSTANCE = os.getenv("LAKEBASE_INSTANCE_NAME")
SCHEMA = os.getenv("LAKEBASE_AGENT_MEMORY_SCHEMA", "memories")
EMBEDDING = os.getenv("DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en")
DEFAULT_USER_ID = os.getenv("PI_MEMORY_DEFAULT_USER_ID", "pi_user")
PORT = int(os.getenv("PI_MEMORY_PORT", "8765"))


def sanitize_user_id(user_id: Optional[str]) -> str:
    """LangGraph store namespace labels cannot contain periods (and must be
    non-empty). Coerce any id into a safe label: keep [A-Za-z0-9_-], map the rest
    to '_'. Must match whatever the transcript writer uses for ai_chatbot.Chat.userId.
    """
    uid = (user_id or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
    return re.sub(r"[^A-Za-z0-9_-]", "_", uid)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not INSTANCE:
        raise RuntimeError("LAKEBASE_INSTANCE_NAME is required")
    async with AsyncDatabricksStore(
        instance_name=INSTANCE,
        embedding_endpoint=EMBEDDING,
        embedding_dims=1024,
        embedding_fields=["content"],
        schema=SCHEMA,
    ) as store:
        await store.setup()
        app.state.store = store
        yield


app = FastAPI(title="pi-memory-sidecar", lifespan=lifespan)


class InvokeRequest(BaseModel):
    tool: str
    user_id: Optional[str] = None
    args: dict[str, Any] = {}


class PreambleRequest(BaseModel):
    user_id: Optional[str] = None


def _user_ns(user_id: Optional[str]) -> tuple[str, str]:
    return (mc.USER_NAMESPACE_PREFIX, sanitize_user_id(user_id))


# Map tool name -> async callable(store, namespace, args) -> str
async def _dispatch(tool: str, store, user_id: Optional[str], args: dict[str, Any]) -> str:
    ns = _user_ns(user_id)
    if tool == "ls_memories":
        return await mc.ls_memories(store, ns, args.get("directory", "/memories/"))
    if tool == "read_memory":
        return await mc.read_memory(store, ns, args["path"])
    if tool == "write_memory":
        return await mc.write_memory(
            store, ns, args["path"], args.get("content", ""),
            args.get("description", ""), args.get("always_load", False),
        )
    if tool == "edit_memory":
        return await mc.edit_memory(store, ns, args["path"], args["old_text"], args["new_text"])
    if tool == "delete_memory":
        return await mc.delete_memory(store, ns, args["path"])
    if tool == "search_user_memories":
        return await mc.search_user_memories(store, ns, args["query"])
    if tool == "ls_agent_memories":
        return await mc.ls_agent_memories(store, args.get("directory", "/memories/"))
    if tool == "read_agent_memory":
        return await mc.read_agent_memory(store, args["path"])
    if tool == "search_agent_memories":
        return await mc.search_agent_memories(store, args["query"])
    raise HTTPException(status_code=404, detail=f"Unknown tool: {tool}")


@app.get("/health")
async def health():
    return {"status": "healthy", "instance": INSTANCE, "schema": SCHEMA}


@app.post("/invoke")
async def invoke(req: InvokeRequest):
    try:
        content = await _dispatch(req.tool, app.state.store, req.user_id, req.args)
        return {"content": content}
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required arg: {e}") from e
    except ValueError as e:
        # path/validation errors surface as a normal tool result, like the real agent
        return {"content": f"Invalid input: {e}"}


@app.post("/preamble")
async def preamble(req: PreambleRequest):
    text = await mc.build_memory_preamble(app.state.store, _user_ns(req.user_id))
    return {"text": text}


def main():
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
