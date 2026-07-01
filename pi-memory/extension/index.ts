/**
 * pi-memory extension (Phase 4)
 *
 * Wires pi (hosted Claude) into the repo's dual-layer Lakebase memory:
 *
 *   pi (Claude) ──tools──► this extension ──HTTP──► sidecar (FastAPI) ──► Lakebase
 *
 * Responsibilities:
 *   - session_start:    spawn the Python sidecar (one long-lived AsyncDatabricksStore),
 *                       wait for /health, then cache the session-start memory preamble.
 *   - registerTool x9:  the same filesystem-style memory tools as the real agent
 *                       (utils_memory.py), each forwarding to the sidecar /invoke.
 *   - before_agent_start: inject the cached memory snapshot into the system prompt,
 *                       mirroring build_memory_preamble() in the stateful agent.
 *   - session_shutdown: flush the session transcript to Lakebase (ai_chatbot.Chat/
 *                       Message) so the dreamer jobs can distill it, then stop the
 *                       sidecar.
 *
 * Load it with:  pi -e pi-memory/extension/index.ts
 * or symlink/copy into .pi/extensions/ for auto-discovery + /reload.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const EXT_DIR = dirname(fileURLToPath(import.meta.url));
const PI_MEMORY_DIR = resolve(EXT_DIR, "..");
const SIDECAR_DIR = join(PI_MEMORY_DIR, "sidecar");

// ---------------------------------------------------------------------------
// Config: defaults mirror .env.example. We read pi-memory/.env (if present) so
// the extension and the sidecar agree on port + user id, then pass everything
// through to the spawned sidecar as process env.
// ---------------------------------------------------------------------------
function parseDotEnv(path: string): Record<string, string> {
  const out: Record<string, string> = {};
  if (!existsSync(path)) return out;
  for (const raw of readFileSync(path, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (key) out[key] = val;
  }
  return out;
}

const dotenv = parseDotEnv(join(PI_MEMORY_DIR, ".env"));
function cfg(key: string, fallback: string): string {
  return process.env[key] ?? dotenv[key] ?? fallback;
}

const PORT = cfg("PI_MEMORY_PORT", "8765");
const BASE_URL = `http://127.0.0.1:${PORT}`;
// The sidecar sanitizes this defensively; PI_MEMORY_USER_ID takes precedence over
// the sidecar's PI_MEMORY_DEFAULT_USER_ID fallback. Keep it consistent with the
// transcript writer's ai_chatbot.Chat.userId (Phase 5).
const USER_ID = cfg("PI_MEMORY_USER_ID", cfg("PI_MEMORY_DEFAULT_USER_ID", ""));
// Opt-in by default. PI_MEMORY_ENABLED=1/true/yes/on flips the default to on; the
// --memory CLI flag (registered below) overrides the env-derived default per launch.
const ENV_DEFAULT = /^(1|true|yes|on)$/i.test(cfg("PI_MEMORY_ENABLED", ""));

// ---------------------------------------------------------------------------
// Sidecar lifecycle (session-scoped; never started from the factory).
// ---------------------------------------------------------------------------
let child: ChildProcess | undefined;
let intentionalStop = false; // true while we SIGTERM the sidecar ourselves
let stderrTail = "";
let preamble = ""; // cached session-start memory snapshot
let enabled = false; // is the memory system active for this session?
const memoryToolNames: string[] = []; // populated when the 9 tools are registered

async function getHealthy(timeoutMs: number, signal?: AbortSignal): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (signal?.aborted) return false;
    try {
      const res = await fetch(`${BASE_URL}/health`, { method: "GET" });
      if (res.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function startSidecar(ctx: ExtensionContext): Promise<boolean> {
  // Idempotent: if something is already serving on the port, reuse it.
  if (await getHealthy(800)) {
    return true;
  }

  const cmd = cfg("PI_MEMORY_SIDECAR_CMD", "uv");
  const args = cfg("PI_MEMORY_SIDECAR_ARGS", "run python server.py").split(/\s+/);

  ctx.ui.setStatus("pi-memory", "starting sidecar…");
  intentionalStop = false;
  child = spawn(cmd, args, {
    cwd: SIDECAR_DIR,
    env: { ...process.env, ...dotenv },
    stdio: ["ignore", "ignore", "pipe"],
  });
  child.stderr?.on("data", (buf: Buffer) => {
    stderrTail = (stderrTail + buf.toString()).slice(-4000);
  });
  child.on("exit", (code) => {
    if (!intentionalStop && code && code !== 0) {
      ctx.ui.notify(`pi-memory sidecar exited (code ${code})`, "error");
    }
    child = undefined;
  });

  const healthy = await getHealthy(45_000);
  if (!healthy) {
    ctx.ui.setStatus("pi-memory", "");
    const tail = stderrTail.trim().split("\n").slice(-6).join("\n");
    ctx.ui.notify(
      `pi-memory sidecar failed to become healthy.\n${tail || "(no stderr)"}`,
      "error",
    );
    stopSidecar();
    return false;
  }
  return true;
}

function stopSidecar(): void {
  if (child && !child.killed) {
    intentionalStop = true;
    child.kill("SIGTERM");
  }
  child = undefined;
}

// ---------------------------------------------------------------------------
// Sidecar HTTP calls.
// ---------------------------------------------------------------------------
async function invoke(
  tool: string,
  args: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<string> {
  const body: Record<string, unknown> = { tool, args };
  if (USER_ID) body.user_id = USER_ID;
  const res = await fetch(`${BASE_URL}/invoke`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`sidecar ${res.status}: ${detail.slice(0, 500)}`);
  }
  const data = (await res.json()) as { content?: string };
  return data.content ?? "";
}

// Reduce a pi session's entries to plain user/assistant text turns, matching the
// shape seed_mock_chat_history.py writes (text-only parts). Thinking blocks, tool
// calls, tool results, and extension/system messages are dropped.
type TranscriptTurn = { id: string; role: string; text: string; created_at?: string };

function extractTurns(entries: readonly any[]): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  for (const entry of entries) {
    if (!entry || entry.type !== "message") continue;
    const msg = entry.message;
    const role = msg?.role;
    if (role !== "user" && role !== "assistant") continue;

    let text = "";
    if (typeof msg.content === "string") {
      text = msg.content;
    } else if (Array.isArray(msg.content)) {
      text = msg.content
        .filter((b: any) => b && b.type === "text" && typeof b.text === "string")
        .map((b: any) => b.text)
        .join("");
    }
    if (!text.trim()) continue; // skip image-only / tool-call-only turns
    turns.push({ id: String(entry.id), role, text, created_at: entry.timestamp });
  }
  return turns;
}

async function postTranscript(
  chatId: string,
  title: string,
  turns: TranscriptTurn[],
): Promise<number> {
  const body: Record<string, unknown> = { chat_id: chatId, title, messages: turns };
  if (USER_ID) body.user_id = USER_ID;
  const res = await fetch(`${BASE_URL}/transcript`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`sidecar ${res.status}: ${detail.slice(0, 500)}`);
  }
  const data = (await res.json()) as { written?: number };
  return data.written ?? 0;
}

async function fetchPreamble(): Promise<string> {
  const body: Record<string, unknown> = {};
  if (USER_ID) body.user_id = USER_ID;
  const res = await fetch(`${BASE_URL}/preamble`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) return "";
  const data = (await res.json()) as { text?: string };
  return data.text ?? "";
}

// ---------------------------------------------------------------------------
// Extension entrypoint.
// ---------------------------------------------------------------------------
export default function (pi: ExtensionAPI) {
  // -------------------------------------------------------------------------
  // Enable / disable. Memory is opt-in: tools stay inactive and the sidecar
  // stays down until enabled via --memory, PI_MEMORY_ENABLED, or /memory on.
  // -------------------------------------------------------------------------
  async function enableMemory(ctx: ExtensionContext): Promise<boolean> {
    if (enabled && child) return true;
    const ok = await startSidecar(ctx);
    if (!ok) {
      enabled = false;
      return false;
    }
    preamble = await fetchPreamble().catch(() => "");
    pi.setActiveTools([...new Set([...pi.getActiveTools(), ...memoryToolNames])]);
    enabled = true;
    const where = USER_ID ? `user ${USER_ID}` : "default user";
    ctx.ui.setStatus("pi-memory", `memory on (${where})`);
    return true;
  }

  function disableMemory(ctx: ExtensionContext): void {
    stopSidecar();
    preamble = "";
    enabled = false;
    const names = new Set(memoryToolNames);
    pi.setActiveTools(pi.getActiveTools().filter((n) => !names.has(n)));
    ctx.ui.setStatus("pi-memory", "memory off");
  }

  // Launch flag: `pi --memory` (or `--memory=false`). Default follows env.
  pi.registerFlag("memory", {
    description: "Enable the Lakebase memory system for the session (opt-in)",
    type: "boolean",
    default: ENV_DEFAULT,
  });

  // In-session toggle: /memory on | off | status
  pi.registerCommand("memory", {
    description: "Enable/disable the Lakebase memory system (on | off | status)",
    getArgumentCompletions: (prefix: string) => {
      const items = ["on", "off", "status"].map((v) => ({ value: v, label: v }));
      const filtered = items.filter((i) => i.value.startsWith(prefix.trim()));
      return filtered.length > 0 ? filtered : null;
    },
    handler: async (args, ctx) => {
      const a = (args || "").trim().toLowerCase();
      if (a === "on" || a === "enable") {
        const ok = await enableMemory(ctx);
        ctx.ui.notify(ok ? "Memory enabled." : "Memory failed to start (see error).", ok ? "info" : "error");
      } else if (a === "off" || a === "disable") {
        disableMemory(ctx);
        ctx.ui.notify("Memory disabled.", "info");
      } else {
        const where = USER_ID ? ` (user ${USER_ID})` : "";
        ctx.ui.notify(
          enabled ? `Memory is ON${where}.` : "Memory is OFF. Run /memory on to enable.",
          "info",
        );
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    if (pi.getFlag("memory") === true) {
      await enableMemory(ctx);
    } else {
      // Tools register active by default — deactivate them until opted in.
      disableMemory(ctx);
    }
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    // Flush the transcript before tearing the sidecar down. Best-effort: a failed
    // write must never block shutdown, and an empty session is silently skipped.
    if (enabled && child) {
      try {
        const sm = ctx.sessionManager;
        const turns = extractTurns(sm.getEntries());
        if (turns.length > 0) {
          const chatId = sm.getSessionId();
          const title = sm.getSessionName() || turns[0].text.slice(0, 80);
          const written = await postTranscript(chatId, title, turns);
          ctx.ui.setStatus("pi-memory", `transcript saved (${written} turns)`);
        }
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        ctx.ui.notify(`pi-memory transcript write failed: ${msg}`, "error");
      }
    }
    stopSidecar();
    enabled = false;
  });

  // Inject the session-start memory snapshot into the system prompt, matching the
  // real agent. The snapshot is captured when memory is enabled (intentionally
  // stable, like build_memory_preamble's "snapshot at session start").
  pi.on("before_agent_start", async (event) => {
    if (!enabled || !preamble) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${preamble}` };
  });

  // -------------------------------------------------------------------------
  // The 9 memory tools — descriptions mirror utils_memory.py so behavior and
  // conventions match the stateful agent the dreamer jobs were built around.
  // -------------------------------------------------------------------------
  type ToolDef = {
    name: string;
    label: string;
    description: string;
    promptSnippet: string;
    parameters: ReturnType<typeof Type.Object>;
    buildArgs: (p: Record<string, unknown>) => Record<string, unknown>;
  };

  const dirParam = Type.String({
    description: 'Path prefix to list, e.g. "/memories/" or "/memories/semantic/".',
  });
  const pathParam = Type.String({
    description: 'Full path under /memories/, ending with .md.',
  });
  const queryParam = Type.String({
    description: "Natural-language description of what you want to find.",
  });

  const tools: ToolDef[] = [
    {
      name: "ls_memories",
      label: "List Memories",
      description:
        "List your memory files under a directory. Returns a markdown listing of paths " +
        'and content size. Use "/memories/" to see everything you have saved, then ' +
        "read_memory(path) to open one.",
      promptSnippet: "List your saved memory files under a directory",
      parameters: Type.Object({ directory: Type.Optional(dirParam) }),
      buildArgs: (p) => ({ directory: p.directory ?? "/memories/" }),
    },
    {
      name: "read_memory",
      label: "Read Memory",
      description:
        "Read the full content of a memory file by exact path, e.g. " +
        '"/memories/semantic/coding_preferences.md". Returns the markdown body or an ' +
        "error if the file does not exist.",
      promptSnippet: "Read the full content of one memory file",
      parameters: Type.Object({ path: pathParam }),
      buildArgs: (p) => ({ path: p.path }),
    },
    {
      name: "write_memory",
      label: "Write Memory",
      description:
        "Create a new memory file or completely overwrite an existing one. content " +
        "REPLACES the file (use edit_memory for surgical changes). description is an " +
        "optional one-line summary shown in the session-start memory map (keep under " +
        "100 chars). always_load=true injects the full content into the system prompt " +
        "at the start of EVERY future session for this user — use sparingly. " +
        "Convention: /memories/episodic/ for events, /memories/semantic/ for timeless " +
        "facts/preferences, /memories/procedural/ for how-to workflows and rules.",
      promptSnippet: "Create or overwrite a memory file",
      parameters: Type.Object({
        path: pathParam,
        content: Type.String({ description: "The full markdown body to store." }),
        description: Type.Optional(
          Type.String({ description: "Optional one-line summary (<100 chars)." }),
        ),
        always_load: Type.Optional(
          Type.Boolean({
            description: "Inject full content into every future session's prompt. Use sparingly.",
          }),
        ),
      }),
      buildArgs: (p) => ({
        path: p.path,
        content: p.content,
        description: p.description ?? "",
        always_load: p.always_load ?? false,
      }),
    },
    {
      name: "edit_memory",
      label: "Edit Memory",
      description:
        "Make a surgical edit to an existing memory file by exact string replacement. " +
        "old_text must appear exactly once (include surrounding context to make it " +
        'unique); new_text replaces it (use "" to delete). Prefer this over ' +
        "write_memory when updating part of a long file.",
      promptSnippet: "Surgically edit part of a memory file",
      parameters: Type.Object({
        path: pathParam,
        old_text: Type.String({ description: "Exact substring to find (must be unique)." }),
        new_text: Type.String({ description: 'Replacement text. Use "" to delete.' }),
      }),
      buildArgs: (p) => ({ path: p.path, old_text: p.old_text, new_text: p.new_text }),
    },
    {
      name: "delete_memory",
      label: "Delete Memory",
      description: "Delete a memory file by exact path.",
      promptSnippet: "Delete a memory file by path",
      parameters: Type.Object({ path: pathParam }),
      buildArgs: (p) => ({ path: p.path }),
    },
    {
      name: "search_user_memories",
      label: "Search User Memories",
      description:
        "Semantic search across all your memory files. Returns the top 5 most relevant " +
        "files with path and a snippet. Use this as your first action on any user turn " +
        "to look up relevant context.",
      promptSnippet: "Semantic search across your memory files",
      parameters: Type.Object({ query: queryParam }),
      buildArgs: (p) => ({ query: p.query }),
    },
    {
      name: "ls_agent_memories",
      label: "List Agent Memories",
      description:
        "List files in the agent's shared knowledge base under a directory. These files " +
        "apply to all users and are managed by admins (read-only at runtime).",
      promptSnippet: "List shared agent knowledge files",
      parameters: Type.Object({ directory: Type.Optional(dirParam) }),
      buildArgs: (p) => ({ directory: p.directory ?? "/memories/" }),
    },
    {
      name: "read_agent_memory",
      label: "Read Agent Memory",
      description:
        "Read the full content of a file in the agent's shared knowledge base, e.g. " +
        '"/memories/procedural/money_formatting.md".',
      promptSnippet: "Read one shared agent knowledge file",
      parameters: Type.Object({ path: pathParam }),
      buildArgs: (p) => ({ path: p.path }),
    },
    {
      name: "search_agent_memories",
      label: "Search Agent Memories",
      description:
        "Semantic search across the agent's shared knowledge base. Returns the top 5 " +
        "most relevant shared knowledge files. These rules apply to all users — run " +
        "this at the start of a turn to surface relevant formatting, currency, and tone " +
        "rules.",
      promptSnippet: "Semantic search across shared agent knowledge",
      parameters: Type.Object({ query: queryParam }),
      buildArgs: (p) => ({ query: p.query }),
    },
  ];

  for (const t of tools) {
    memoryToolNames.push(t.name);
    pi.registerTool({
      name: t.name,
      label: t.label,
      description: t.description,
      promptSnippet: t.promptSnippet,
      parameters: t.parameters,
      async execute(_toolCallId, params, signal) {
        if (!enabled) {
          return {
            content: [
              { type: "text", text: "Memory is disabled for this session. Run /memory on to enable it." },
            ],
            details: {},
          };
        }
        try {
          const text = await invoke(t.name, t.buildArgs(params as Record<string, unknown>), signal);
          return { content: [{ type: "text", text }], details: {} };
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          return {
            content: [{ type: "text", text: `Memory tool error: ${msg}` }],
            details: {},
            isError: true,
          };
        }
      },
    });
  }
}
