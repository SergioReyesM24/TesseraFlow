import json
import uuid
from collections import Counter
from importlib.resources import files
from typing import Any

import asyncpg

from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
)
from application.ports import ConversationRepository
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.tools import ToolCall, ToolResult
from infrastructure.conversation_codec import (
    decode_conversation_item,
    encode_conversation_item,
)

SELECT_CONVERSATION = """
SELECT user_id, tenant_id, title, version, last_sequence
FROM conversations
WHERE id = $1
"""

SELECT_CONVERSATION_FOR_UPDATE = SELECT_CONVERSATION + " FOR UPDATE"

SELECT_RECENT_ITEMS = """
WITH recent_turns AS (
    SELECT DISTINCT turn_id
    FROM (
        SELECT turn_id, sequence
        FROM conversation_items
        WHERE conversation_id = $1
        ORDER BY sequence DESC
        LIMIT $2
    ) AS recent_items
)
SELECT item.payload
FROM conversation_items AS item
JOIN recent_turns USING (turn_id)
WHERE item.conversation_id = $1
ORDER BY item.sequence
"""

INSERT_CONVERSATION = """
INSERT INTO conversations (
    id, user_id, tenant_id, title, version, last_sequence, last_message_at
)
VALUES ($1, $2, $3, $4, 0, 0, NOW())
"""

INSERT_ITEM = """
INSERT INTO conversation_items (
    conversation_id, turn_id, sequence, item_type, role, call_id, tool_name, payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
"""

UPDATE_CONVERSATION = """
UPDATE conversations
SET version = $2,
    last_sequence = $3,
    updated_at = NOW(),
    last_message_at = NOW()
WHERE id = $1
"""


class InvalidPostgresConversationDataError(RuntimeError):
    """Raised when canonical rows cannot be translated to domain history."""


class PostgresConversationRepository(ConversationRepository):
    """Canonical append-only conversation persistence backed by PostgreSQL."""

    def __init__(self, pool: asyncpg.Pool, *, context_item_limit: int) -> None:
        """Bind a shared pool and bound the recent context loaded per conversation."""
        if context_item_limit < 2:
            raise ValueError("context_item_limit must be at least 2")
        self._pool = pool
        self._context_item_limit = context_item_limit

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Load recent complete turns after validating canonical ownership."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(SELECT_CONVERSATION, key.conversation_id)
            if row is None:
                return None
            self._validate_owner(key, row)
            item_rows = await connection.fetch(
                SELECT_RECENT_ITEMS,
                key.conversation_id,
                self._context_item_limit,
            )
        try:
            messages = tuple(self._decode_payload(item_row["payload"]) for item_row in item_rows)
            version = int(row["version"])
            title = row["title"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidPostgresConversationDataError(
                "Canonical conversation data is invalid"
            ) from exc
        if version < 1 or title is not None and not isinstance(title, str):
            raise InvalidPostgresConversationDataError("Canonical conversation fields are invalid")
        return Conversation(key=key, messages=messages, version=version, title=title)

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Append one complete turn under a row lock and optimistic version check."""
        self._validate_turn(turn)
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                row = await connection.fetchrow(
                    SELECT_CONVERSATION_FOR_UPDATE,
                    conversation.key.conversation_id,
                )
                if row is None:
                    if conversation.version != 0:
                        raise ConversationConflictError(
                            "Conversation was updated by another request"
                        )
                    title = self._default_title(turn)
                    await connection.execute(
                        INSERT_CONVERSATION,
                        conversation.key.conversation_id,
                        conversation.key.user_id,
                        conversation.key.tenant_id,
                        title,
                    )
                    last_sequence = 0
                else:
                    self._validate_owner(conversation.key, row)
                    if int(row["version"]) != conversation.version:
                        raise ConversationConflictError(
                            "Conversation was updated by another request"
                        )
                    title = row["title"]
                    last_sequence = int(row["last_sequence"])

                turn_id = uuid.uuid4()
                records = [
                    self._item_record(
                        conversation.key.conversation_id,
                        turn_id,
                        last_sequence + offset,
                        item,
                    )
                    for offset, item in enumerate(turn, start=1)
                ]
                await connection.executemany(INSERT_ITEM, records)
                version = conversation.version + 1
                await connection.execute(
                    UPDATE_CONVERSATION,
                    conversation.key.conversation_id,
                    version,
                    last_sequence + len(turn),
                )
        except asyncpg.UniqueViolationError as exc:
            raise ConversationConflictError("Conversation was updated by another request") from exc

        return Conversation(
            key=conversation.key,
            messages=conversation.messages + turn,
            version=version,
            title=title,
        )

    async def delete(self, key: ConversationKey) -> bool:
        """Delete the owned canonical row and cascade all of its history items."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(SELECT_CONVERSATION_FOR_UPDATE, key.conversation_id)
            if row is None:
                return False
            self._validate_owner(key, row)
            await connection.execute("DELETE FROM conversations WHERE id = $1", key.conversation_id)
        return True

    @staticmethod
    def _validate_owner(key: ConversationKey, row: asyncpg.Record) -> None:
        """Reject reads and writes performed by a different user or tenant."""
        if row["user_id"] != key.user_id or row["tenant_id"] != key.tenant_id:
            raise ConversationAccessDeniedError("Conversation ownership does not match")

    @staticmethod
    def _validate_turn(turn: tuple[ConversationItem, ...]) -> None:
        """Require a complete user-to-assistant turn with matched tool results."""
        if len(turn) < 2:
            raise ValueError("A conversation turn must contain at least two items")
        first, last = turn[0], turn[-1]
        if not isinstance(first, ConversationMessage) or first.role != "user":
            raise ValueError("A conversation turn must start with a user message")
        if not isinstance(last, ConversationMessage) or last.role != "assistant":
            raise ValueError("A conversation turn must end with an assistant message")
        if any(isinstance(item, ConversationMessage) for item in turn[1:-1]):
            raise ValueError("Conversation messages may only delimit a persisted turn")
        calls = Counter(item.call_id for item in turn if isinstance(item, ToolCall))
        results = Counter(item.call_id for item in turn if isinstance(item, ToolResult))
        if calls != results or any(count != 1 for count in calls.values()):
            raise ValueError("Every tool call must have exactly one matching result")

    @staticmethod
    def _default_title(turn: tuple[ConversationItem, ...]) -> str:
        """Derive a small initial title without requiring another model request."""
        first = turn[0]
        assert isinstance(first, ConversationMessage)
        normalized = " ".join(first.content.split())
        return normalized[:120] or "Nueva conversación"

    @staticmethod
    def _item_record(
        conversation_id: str,
        turn_id: uuid.UUID,
        sequence: int,
        item: ConversationItem,
    ) -> tuple[object, ...]:
        """Build one ordered SQL record while keeping the full neutral JSON payload."""
        encoded = encode_conversation_item(item)
        item_type = str(encoded["type"])
        role = item.role if isinstance(item, ConversationMessage) else None
        call_id = item.call_id if isinstance(item, ToolCall | ToolResult) else None
        tool_name = item.tool_name if isinstance(item, ToolCall) else None
        return (
            conversation_id,
            turn_id,
            sequence,
            item_type,
            role,
            call_id,
            tool_name,
            json.dumps(encoded, ensure_ascii=False, default=str, separators=(",", ":")),
        )

    @staticmethod
    def _decode_payload(payload: Any) -> ConversationItem:
        """Decode asyncpg JSONB values whether configured as strings or objects."""
        raw = json.loads(payload) if isinstance(payload, str) else payload
        return decode_conversation_item(raw)


async def apply_postgres_migrations(pool: asyncpg.Pool) -> None:
    """Apply bundled SQL migrations once under a PostgreSQL advisory lock."""
    migration_root = files("infrastructure.migrations")
    migration_names = sorted(
        entry.name for entry in migration_root.iterdir() if entry.name.endswith(".sql")
    )
    async with pool.acquire() as connection:
        await connection.execute("SELECT pg_advisory_lock(hashtext('tesseraflow_migrations'))")
        try:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            applied_rows = await connection.fetch("SELECT name FROM schema_migrations")
            applied = {str(row["name"]) for row in applied_rows}
            for name in migration_names:
                if name in applied:
                    continue
                sql = migration_root.joinpath(name).read_text(encoding="utf-8")
                async with connection.transaction():
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)", name
                    )
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtext('tesseraflow_migrations'))"
            )
