"""Transcript writer: persist a pi session's turns into ``ai_chatbot`` tables.

Phase 5 of the pi-memory testbed. The pi extension collects the session's
user/assistant turns on ``session_shutdown`` and POSTs them to the sidecar,
which writes them here so the dreamer batch jobs can distill them.

The ``Chat`` / ``Message`` schema mirrors ``agent-stateful-example``'s
``scripts/seed_mock_chat_history.py`` exactly, so the dreamer's read query works
unchanged against the provisioned instance:

    SELECT c."userId", m."chatId", m.role, m.parts, m."createdAt"
    FROM ai_chatbot."Message" m
    JOIN ai_chatbot."Chat" c ON m."chatId" = c.id
    WHERE m."createdAt" >= NOW() - INTERVAL '1 day'

Writes are keyed by the pi session id (``chatId`` == ``Chat.id``) and are
idempotent: re-writing the same session replaces its prior rows, so a session
that shuts down more than once (fork/clone/reload) never double-counts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from databricks_ai_bridge.lakebase import LakebaseClient

# Fixed to match seed_mock_chat_history.py and the dreamer read query, which both
# hardcode ai_chatbot. Exposed as a constant rather than a config knob because the
# DDL/DML below embed the schema literally.
CHAT_SCHEMA = "ai_chatbot"

# One-time, idempotent DDL. Columns/quoting match seed_mock_chat_history.py so the
# dreamer read query is portable between the app's Lakebase branch and this
# provisioned instance. ``json`` (not ``jsonb``) matches the app's Prisma schema.
_DDL = (
    "CREATE SCHEMA IF NOT EXISTS ai_chatbot",
    """
    CREATE TABLE IF NOT EXISTS ai_chatbot."Chat" (
        id            text PRIMARY KEY,
        "createdAt"   timestamptz NOT NULL,
        title         text,
        "userId"      text NOT NULL,
        visibility    text,
        "lastContext" json
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_chatbot."Message" (
        id            text PRIMARY KEY,
        "chatId"      text NOT NULL,
        role          text NOT NULL,
        parts         json NOT NULL,
        attachments   json,
        "createdAt"   timestamptz NOT NULL,
        "traceId"     text
    )
    """,
    'CREATE INDEX IF NOT EXISTS "Message_chatId_idx" ON ai_chatbot."Message" ("chatId")',
)


@dataclass
class TranscriptTurn:
    """One user or assistant turn, already reduced to plain text by the caller."""

    id: str
    role: str
    text: str
    created_at: Optional[str] = None  # ISO-8601; falls back to now() when absent


def _parse_ts(value: Optional[str], fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        # Accept trailing "Z" (Python < 3.11 fromisoformat rejects it).
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback


def _text_parts(text: str) -> str:
    """Serialize a turn's text into the ``parts`` JSON shape the dreamer expects."""
    return json.dumps([{"type": "text", "text": text}])


def _write_sync(
    instance_name: str,
    chat_id: str,
    user_id: str,
    title: str,
    turns: list[TranscriptTurn],
) -> int:
    now = datetime.now(timezone.utc)
    chat_created = _parse_ts(turns[0].created_at if turns else None, now)

    with LakebaseClient(instance_name=instance_name) as client:
        for stmt in _DDL:
            client.execute(stmt)

        # Idempotent replace: clear any prior rows for this session, then insert.
        client.execute('DELETE FROM ai_chatbot."Message" WHERE "chatId" = %s', (chat_id,))
        client.execute('DELETE FROM ai_chatbot."Chat" WHERE id = %s', (chat_id,))

        client.execute(
            'INSERT INTO ai_chatbot."Chat" '
            '(id, "createdAt", title, "userId", visibility, "lastContext") '
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (chat_id, chat_created, title or None, user_id, "private", None),
        )

        for turn in turns:
            client.execute(
                'INSERT INTO ai_chatbot."Message" '
                '(id, "chatId", role, parts, attachments, "createdAt", "traceId") '
                "VALUES (%s, %s, %s, %s::json, %s::json, %s, %s)",
                (
                    turn.id,
                    chat_id,
                    turn.role,
                    _text_parts(turn.text),
                    json.dumps([]),
                    _parse_ts(turn.created_at, now),
                    None,
                ),
            )

    return len(turns)


async def write_transcript(
    instance_name: str,
    chat_id: str,
    user_id: str,
    title: str,
    turns: list[TranscriptTurn],
) -> int:
    """Async wrapper around the blocking psycopg writes (LakebaseClient is sync)."""
    if not turns:
        return 0
    return await asyncio.to_thread(_write_sync, instance_name, chat_id, user_id, title, turns)


def coerce_turns(raw: list[dict[str, Any]]) -> list[TranscriptTurn]:
    """Validate + normalize the extension's message payload into TranscriptTurns.

    Skips anything without a non-empty text body or a user/assistant role, so the
    stored transcript matches the seeded conversations (text turns only).
    """
    turns: list[TranscriptTurn] = []
    for i, item in enumerate(raw):
        role = str(item.get("role", "")).strip().lower()
        text = item.get("text", "")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            continue
        turns.append(
            TranscriptTurn(
                id=str(item.get("id") or f"{i}"),
                role=role,
                text=text,
                created_at=item.get("created_at"),
            )
        )
    return turns
