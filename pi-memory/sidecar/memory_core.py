"""Framework-agnostic memory operations.

A faithful port of the tool bodies in
``agent-stateful-example/agent_server/utils_memory.py``, but as plain async
functions that take a ``store`` + ``namespace`` instead of LangChain ``@tool``
closures. The FastAPI sidecar (server.py) exposes these to pi; the dreamer jobs
read/write the same store, so outputs and conventions must match the real agent.

Memory layout convention (paths under /memories/, ending in .md):
  /memories/episodic/    events / what happened (timestamps, conversations)
  /memories/semantic/    facts, preferences, identity (timeless)
  /memories/procedural/  how-to workflows and rules
"""

from typing import Any, Optional

from langgraph.store.base import BaseStore

MEMORY_ROOT = "/memories/"
USER_NAMESPACE_PREFIX = "user_memories"
AGENT_NAMESPACE_PREFIX = "agent_memories"
AGENT_NAMESPACE = (AGENT_NAMESPACE_PREFIX,)


# ---------------------------------------------------------------------------
# Path / value helpers (ported verbatim from utils_memory.py)
# ---------------------------------------------------------------------------
def normalize_path(path: str) -> str:
    """Normalize a memory file path. Returns the cleaned path or raises ValueError."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    if not p.startswith(MEMORY_ROOT):
        raise ValueError(f"path must start with '{MEMORY_ROOT}' (got: {path!r})")
    if not p.endswith(".md"):
        raise ValueError(f"path must end with '.md' (got: {path!r})")
    if "//" in p or p.endswith("/"):
        raise ValueError(f"path contains empty segments (got: {path!r})")
    return p


def normalize_directory(directory: str) -> str:
    """Normalize a directory prefix. Always returns a string ending in '/'."""
    if not isinstance(directory, str) or not directory.strip():
        return MEMORY_ROOT
    d = directory.strip()
    if not d.startswith("/"):
        d = "/" + d
    if not d.startswith(MEMORY_ROOT) and d != MEMORY_ROOT.rstrip("/"):
        d = MEMORY_ROOT + d.lstrip("/")
    if not d.endswith("/"):
        d = d + "/"
    return d


def _value_field(value: Any, field: str, default: Any = "") -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return default


def _format_search_result(item: Any, snippet_chars: int = 400) -> str:
    content = _value_field(item.value, "content", "")
    description = _value_field(item.value, "description", "")
    snippet = content if len(content) <= snippet_chars else content[:snippet_chars] + "..."
    score = getattr(item, "score", None)
    score_str = f" (score={score:.3f})" if isinstance(score, float) else ""
    desc_str = f"\n_{description}_\n" if description else ""
    return f"## {item.key}{score_str}{desc_str}\n{snippet}"


def _format_listing(items: list[Any]) -> str:
    if not items:
        return "(empty)"
    lines = []
    for item in items:
        content = _value_field(item.value, "content", "")
        description = _value_field(item.value, "description", "")
        flag = " [always-loaded]" if _value_field(item.value, "startup_load", False) else ""
        desc = f" — {description}" if description else ""
        lines.append(f"- `{item.key}` ({len(content)} chars){flag}{desc}")
    return "\n".join(lines)


def _build_memory_map(items: list[Any], heading: str) -> str:
    if not items:
        return f"## {heading}\n\n(empty)\n"
    items = sorted(items, key=lambda it: it.key)
    lines = [f"## {heading}", ""]
    for item in items:
        description = _value_field(item.value, "description", "")
        flag = " [always-loaded]" if _value_field(item.value, "startup_load", False) else ""
        desc = f" — {description}" if description else ""
        lines.append(f"- `{item.key}`{flag}{desc}")
    return "\n".join(lines) + "\n"


def _build_startup_load_section(items: list[Any], heading: str) -> str:
    always_on = [it for it in items if _value_field(it.value, "startup_load", False)]
    if not always_on:
        return ""
    always_on.sort(key=lambda it: it.key)
    lines = [f"## {heading}", ""]
    for item in always_on:
        content = _value_field(item.value, "content", "")
        lines.append(f"### `{item.key}`\n\n{content}".rstrip())
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# User-scoped operations (read/write)
# ---------------------------------------------------------------------------
async def ls_memories(store: BaseStore, namespace: tuple[str, str], directory: str) -> str:
    prefix = normalize_directory(directory)
    items = await store.asearch(namespace, limit=200)
    matching = [it for it in items if it.key.startswith(prefix)]
    if not matching:
        return f"No memory files found under {prefix}"
    matching.sort(key=lambda it: it.key)
    return f"Files under {prefix} ({len(matching)} total):\n" + _format_listing(matching)


async def read_memory(store: BaseStore, namespace: tuple[str, str], path: str) -> str:
    normalized = normalize_path(path)
    item = await store.aget(namespace, normalized)
    if item is None:
        return f"File not found: {normalized}"
    content = item.value.get("content", "") if isinstance(item.value, dict) else ""
    return f"# {normalized}\n\n{content}"


async def write_memory(
    store: BaseStore,
    namespace: tuple[str, str],
    path: str,
    content: str,
    description: str = "",
    always_load: bool = False,
) -> str:
    normalized = normalize_path(path)
    if not isinstance(content, str):
        return "Invalid content: must be a string."
    existing = await store.aget(namespace, normalized)
    value: dict[str, Any] = {"content": content}
    if description:
        value["description"] = description
    if always_load:
        value["startup_load"] = True
    await store.aput(namespace, normalized, value)
    action = "Overwrote" if existing is not None else "Created"
    suffix = " [always-loaded]" if always_load else ""
    return f"{action} {normalized} ({len(content)} chars){suffix}."


async def edit_memory(
    store: BaseStore, namespace: tuple[str, str], path: str, old_text: str, new_text: str
) -> str:
    normalized = normalize_path(path)
    item = await store.aget(namespace, normalized)
    if item is None:
        return f"Cannot edit — file not found: {normalized}. Use write_memory to create it."
    content = item.value.get("content", "") if isinstance(item.value, dict) else ""
    occurrences = content.count(old_text)
    if occurrences == 0:
        return f"old_text not found in {normalized}. The file content has not been changed."
    if occurrences > 1:
        return (
            f"old_text appears {occurrences} times in {normalized}. "
            "Provide more surrounding context so it matches exactly once."
        )
    new_content = content.replace(old_text, new_text, 1)
    new_value: dict[str, Any] = dict(item.value) if isinstance(item.value, dict) else {}
    new_value["content"] = new_content
    await store.aput(namespace, normalized, new_value)
    return f"Edited {normalized} ({len(content)} → {len(new_content)} chars)."


async def delete_memory(store: BaseStore, namespace: tuple[str, str], path: str) -> str:
    normalized = normalize_path(path)
    await store.adelete(namespace, normalized)
    return f"Deleted {normalized}."


async def search_user_memories(store: BaseStore, namespace: tuple[str, str], query: str) -> str:
    results = await store.asearch(namespace, query=query, limit=5)
    if not results:
        return "No memories found matching your query."
    formatted = "\n\n".join(_format_search_result(item) for item in results)
    return f"Top {len(results)} matches for {query!r}:\n\n{formatted}"


# ---------------------------------------------------------------------------
# Agent-scoped operations (read-only, shared across users)
# ---------------------------------------------------------------------------
async def ls_agent_memories(store: BaseStore, directory: str) -> str:
    prefix = normalize_directory(directory)
    items = await store.asearch(AGENT_NAMESPACE, limit=200)
    matching = [it for it in items if it.key.startswith(prefix)]
    if not matching:
        return f"No agent-memory files found under {prefix}"
    matching.sort(key=lambda it: it.key)
    return f"Agent files under {prefix} ({len(matching)} total):\n" + _format_listing(matching)


async def read_agent_memory(store: BaseStore, path: str) -> str:
    normalized = normalize_path(path)
    item = await store.aget(AGENT_NAMESPACE, normalized)
    if item is None:
        return f"Agent file not found: {normalized}"
    content = item.value.get("content", "") if isinstance(item.value, dict) else ""
    return f"# {normalized}\n\n{content}"


async def search_agent_memories(store: BaseStore, query: str) -> str:
    results = await store.asearch(AGENT_NAMESPACE, query=query, limit=5)
    if not results:
        return "No agent memories found matching your query."
    formatted = "\n\n".join(_format_search_result(item) for item in results)
    return f"Top {len(results)} agent matches for {query!r}:\n\n{formatted}"


# ---------------------------------------------------------------------------
# Session-start preamble (memory map + always-loaded files)
# ---------------------------------------------------------------------------
async def build_memory_preamble(
    store: BaseStore,
    user_namespace: Optional[tuple[str, str]],
) -> str:
    """Build a preamble for the system prompt: a map of every user + agent memory
    file (with descriptions) plus the full content of any file marked
    ``startup_load: true``. Uses a single store for both namespaces."""
    sections: list[str] = ["# Your memory snapshot at session start", ""]

    user_items: list[Any] = []
    if user_namespace is not None:
        try:
            user_items = await store.asearch(user_namespace, limit=500)
        except Exception:
            pass
    sections.append(_build_memory_map(user_items, "Your user memory files"))

    agent_items: list[Any] = []
    try:
        agent_items = await store.asearch(AGENT_NAMESPACE, limit=500)
    except Exception:
        pass
    sections.append(_build_memory_map(agent_items, "Shared agent knowledge files"))

    user_startup = _build_startup_load_section(user_items, "Always-loaded: your user memory")
    if user_startup:
        sections.append(user_startup)
    agent_startup = _build_startup_load_section(agent_items, "Always-loaded: shared agent knowledge")
    if agent_startup:
        sections.append(agent_startup)

    return "\n".join(sections).rstrip() + "\n"
