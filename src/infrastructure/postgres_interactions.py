import json
from typing import Any, cast

import asyncpg

from application.interactions import InteractionQueueFullError
from application.ports import InteractionRepository
from domain.conversations import ConversationKey
from domain.interactions import (
    InteractionCommand,
    InteractionCommandKind,
    InteractionCommandStatus,
    InteractionDeliveryMode,
    InteractionModality,
    InteractionOutput,
    InteractionSource,
)
from infrastructure.interaction_codec import decode_agent_event, encode_agent_event

INSERT_COMMAND = """
WITH locked_conversation AS (
    SELECT id
    FROM conversations
    WHERE id = $3
    FOR UPDATE
), pending AS (
    SELECT COUNT(*) AS count
    FROM interaction_commands AS command
    JOIN locked_conversation ON locked_conversation.id = command.conversation_id
    WHERE command.status IN ('queued', 'running')
      AND command.delivery_mode = $7
)
INSERT INTO interaction_commands (
    id, request_id, conversation_id, kind, source, message, delivery_mode, causation_id
)
SELECT $1, $2, locked_conversation.id, $4, $5, $6, $7, $8
FROM locked_conversation, pending
WHERE pending.count < $9
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

CLAIM_NEXT_COMMAND = """
WITH candidate AS (
    SELECT command.id, conversation.user_id
    FROM interaction_commands AS command
    JOIN conversations AS conversation ON conversation.id = command.conversation_id
    WHERE (
        command.status = 'queued'
        OR (command.status = 'running' AND command.lease_expires_at < NOW())
    )
      AND command.delivery_mode = 'turn_based'
      AND NOT EXISTS (
          SELECT 1
          FROM interaction_commands AS earlier
          WHERE earlier.conversation_id = command.conversation_id
            AND earlier.sequence < command.sequence
            AND earlier.delivery_mode = command.delivery_mode
            AND earlier.status IN ('queued', 'running')
      )
    ORDER BY command.sequence
    LIMIT 1
    FOR UPDATE OF command SKIP LOCKED
)
UPDATE interaction_commands AS command
SET status = 'running',
    worker_id = $1,
    lease_expires_at = NOW() + ($2 * INTERVAL '1 second'),
    attempt_count = command.attempt_count + 1,
    started_at = COALESCE(command.started_at, NOW()),
    completed_at = NULL,
    error_code = NULL
FROM candidate
WHERE command.id = candidate.id
RETURNING command.id, command.request_id, command.conversation_id, command.kind,
          command.source, command.message, command.delivery_mode, command.causation_id,
          command.status,
          command.attempt_count, candidate.user_id
"""

CLAIM_NEXT_REALTIME_COMMAND = """
WITH candidate AS (
    SELECT command.id, conversation.user_id
    FROM interaction_commands AS command
    JOIN conversations AS conversation ON conversation.id = command.conversation_id
    WHERE command.conversation_id = $3
      AND conversation.user_id = $4
      AND command.delivery_mode = 'realtime'
      AND (
          command.status = 'queued'
          OR (command.status = 'running' AND command.lease_expires_at < NOW())
      )
      AND NOT EXISTS (
          SELECT 1
          FROM interaction_commands AS earlier
          WHERE earlier.conversation_id = command.conversation_id
            AND earlier.delivery_mode = command.delivery_mode
            AND earlier.sequence < command.sequence
            AND earlier.status IN ('queued', 'running')
      )
    ORDER BY command.sequence
    LIMIT 1
    FOR UPDATE OF command SKIP LOCKED
)
UPDATE interaction_commands AS command
SET status = 'running',
    worker_id = $1,
    lease_expires_at = NOW() + ($2 * INTERVAL '1 second'),
    attempt_count = command.attempt_count + 1,
    started_at = COALESCE(command.started_at, NOW()),
    completed_at = NULL,
    error_code = NULL
FROM candidate
WHERE command.id = candidate.id
RETURNING command.id, command.request_id, command.conversation_id, command.kind,
          command.source, command.message, command.delivery_mode, command.causation_id,
          command.status, command.attempt_count, candidate.user_id
"""

INSERT_OUTPUT = """
INSERT INTO interaction_outbox (
    id, command_id, request_id, conversation_id, modality, event_type, payload
)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
ON CONFLICT (id) DO NOTHING
"""

COMPLETE_COMMAND = """
UPDATE interaction_commands
SET status = 'completed',
    worker_id = NULL,
    lease_expires_at = NULL,
    error_code = NULL,
    completed_at = NOW()
WHERE id = $1 AND worker_id = $2 AND status = 'running'
"""

FAIL_COMMAND = """
UPDATE interaction_commands
SET status = 'failed',
    worker_id = NULL,
    lease_expires_at = NULL,
    error_code = $3,
    completed_at = NOW()
WHERE id = $1 AND worker_id = $2 AND status = 'running'
"""

REQUEUE_COMMAND = """
UPDATE interaction_commands
SET status = 'queued',
    worker_id = NULL,
    lease_expires_at = NULL,
    completed_at = NULL
WHERE id = $1 AND worker_id = $2 AND status = 'running'
"""

SELECT_OUTPUTS = """
SELECT output.sequence, output.id, output.command_id, output.request_id,
       output.conversation_id, output.modality, output.event_type, output.payload,
       conversation.user_id
FROM interaction_outbox AS output
JOIN conversations AS conversation ON conversation.id = output.conversation_id
WHERE output.conversation_id = $1
  AND conversation.user_id = $2
  AND (output.delivered_at IS NULL OR $4::text IS NOT NULL)
  AND output.sequence > $3
  AND ($4::text IS NULL OR output.command_id = $4)
ORDER BY output.sequence
LIMIT $5
"""

ACKNOWLEDGE_OUTPUT = """
UPDATE interaction_outbox AS output
SET delivered_at = NOW()
FROM conversations AS conversation
WHERE output.id = $1
  AND conversation.id = output.conversation_id
  AND output.conversation_id = $2
  AND conversation.user_id = $3
  AND output.delivered_at IS NULL
"""


class PostgresInteractionRepository(InteractionRepository):
    """PostgreSQL inbox/outbox that serializes each interactive conversation."""

    def __init__(self, pool: asyncpg.Pool, *, max_pending_commands: int) -> None:
        """Bind the pool and bound queued user inputs per conversation."""
        if max_pending_commands < 1:
            raise ValueError("max_pending_commands must be positive")
        self._pool = pool
        self._max_pending_commands = max_pending_commands

    async def enqueue(self, command: InteractionCommand) -> None:
        """Insert a command after atomically enforcing the pending-input limit."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                INSERT_COMMAND,
                command.command_id,
                command.request_id,
                command.conversation.conversation_id,
                command.kind,
                command.source,
                command.message,
                command.delivery_mode,
                command.causation_id,
                self._max_pending_commands,
            )
        if row is None:
            raise InteractionQueueFullError("Conversation has too many pending commands")

    async def claim_next(
        self,
        worker_id: str,
        lease_seconds: float,
    ) -> InteractionCommand | None:
        """Lease the globally oldest runnable command without overlapping one chat."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(CLAIM_NEXT_COMMAND, worker_id, lease_seconds)
        return self._command_from_row(row) if row is not None else None

    async def claim_next_realtime(
        self,
        conversation: ConversationKey,
        worker_id: str,
        lease_seconds: float,
    ) -> InteractionCommand | None:
        """Lease one realtime completion through its complete ownership scope."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                CLAIM_NEXT_REALTIME_COMMAND,
                worker_id,
                lease_seconds,
                conversation.conversation_id,
                conversation.user_id,
            )
        return self._command_from_row(row) if row is not None else None

    async def append_output(self, output: InteractionOutput) -> None:
        """Persist a typed event with an idempotent attempt-and-position identifier."""
        event_type, payload = encode_agent_event(output.event)
        async with self._pool.acquire() as connection:
            await connection.execute(
                INSERT_OUTPUT,
                output.output_id,
                output.command_id,
                output.request_id,
                output.conversation.conversation_id,
                output.modality,
                event_type,
                json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")),
            )

    async def complete(self, command_id: str, worker_id: str) -> None:
        """Complete a command only while this coordinator still owns its lease."""
        async with self._pool.acquire() as connection:
            status = await connection.execute(COMPLETE_COMMAND, command_id, worker_id)
        self._require_updated(status, command_id)

    async def fail(self, command_id: str, worker_id: str, error_code: str) -> None:
        """Fail a command only while this coordinator still owns its lease."""
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                FAIL_COMMAND,
                command_id,
                worker_id,
                error_code,
            )
        self._require_updated(status, command_id)

    async def requeue(self, command_id: str, worker_id: str) -> None:
        """Release an interrupted command without converting cancellation to failure."""
        async with self._pool.acquire() as connection:
            await connection.execute(REQUEUE_COMMAND, command_id, worker_id)

    async def load_outputs(
        self,
        conversation: ConversationKey,
        *,
        after_sequence: int,
        command_id: str | None = None,
        limit: int = 100,
    ) -> tuple[InteractionOutput, ...]:
        """Read pending outputs in global order after validating conversation ownership."""
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                SELECT_OUTPUTS,
                conversation.conversation_id,
                conversation.user_id,
                after_sequence,
                command_id,
                limit,
            )
        return tuple(self._output_from_row(row) for row in rows)

    async def acknowledge(self, output_id: str, conversation: ConversationKey) -> None:
        """Mark a successfully sent output delivered through its ownership boundary."""
        async with self._pool.acquire() as connection:
            await connection.execute(
                ACKNOWLEDGE_OUTPUT,
                output_id,
                conversation.conversation_id,
                conversation.user_id,
            )

    @staticmethod
    def _command_from_row(row: Any) -> InteractionCommand:
        """Decode one leased command without leaking database row types."""
        return InteractionCommand(
            command_id=str(row["id"]),
            request_id=str(row["request_id"]),
            conversation=ConversationKey(
                conversation_id=str(row["conversation_id"]),
                user_id=str(row["user_id"]),
            ),
            kind=cast(InteractionCommandKind, str(row["kind"])),
            source=cast(InteractionSource, str(row["source"])),
            message=str(row["message"]),
            delivery_mode=cast(
                InteractionDeliveryMode,
                str(row.get("delivery_mode", "turn_based")),
            ),
            causation_id=(str(row["causation_id"]) if row["causation_id"] is not None else None),
            status=cast(InteractionCommandStatus, str(row["status"])),
            attempt_count=int(row["attempt_count"]),
        )

    @staticmethod
    def _output_from_row(row: Any) -> InteractionOutput:
        """Decode one stored event into a transport- and provider-neutral output."""
        raw_payload = (
            json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        )
        return InteractionOutput(
            output_id=str(row["id"]),
            command_id=str(row["command_id"]),
            request_id=str(row["request_id"]),
            conversation=ConversationKey(
                conversation_id=str(row["conversation_id"]),
                user_id=str(row["user_id"]),
            ),
            modality=cast(InteractionModality, str(row["modality"])),
            event=decode_agent_event(str(row["event_type"]), raw_payload),
            sequence=int(row["sequence"]),
        )

    @staticmethod
    def _require_updated(status: str, command_id: str) -> None:
        """Reject stale coordinator processes whose lease was reclaimed."""
        if status != "UPDATE 1":
            raise RuntimeError(f"Interaction command claim was lost: {command_id}")
