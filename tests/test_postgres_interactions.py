import json

import pytest

from domain.conversations import ConversationKey
from domain.events import AgentTextDelta
from domain.interactions import InteractionCommand, InteractionOutput
from infrastructure.postgres_interactions import (
    ACKNOWLEDGE_OUTPUT,
    CLAIM_NEXT_COMMAND,
    COMPLETE_COMMAND,
    INSERT_COMMAND,
    INSERT_OUTPUT,
    SELECT_OUTPUTS,
    PostgresInteractionRepository,
)


class FakeTransaction:
    """Provide the async context shape used by repository transactions."""

    async def __aenter__(self) -> "FakeTransaction":
        """Enter a no-op transaction."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Exit a no-op transaction."""
        return None


class FakeConnection:
    """Capture SQL and return typed inbox and outbox rows."""

    def __init__(self) -> None:
        """Initialize observations and a successful mutation status."""
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.update_status = "UPDATE 1"

    def transaction(self) -> FakeTransaction:
        """Create one no-op transaction context."""
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        """Record a statement and return its configured status."""
        self.executed.append((query, args))
        return self.update_status

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        """Return one leased command or no row after a simulated miss."""
        self.executed.append((query, args))
        if query == INSERT_COMMAND:
            return {"id": "command-1"}
        if query != CLAIM_NEXT_COMMAND:
            raise AssertionError(f"Unexpected query: {query}")
        return {
            "id": "command-1",
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "kind": "user_message",
            "source": "speech_user",
            "message": "Transcripción",
            "causation_id": None,
            "status": "running",
            "attempt_count": 1,
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        }

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        """Return one pending text event for the exact output query."""
        self.executed.append((query, args))
        if query != SELECT_OUTPUTS:
            raise AssertionError(f"Unexpected query: {query}")
        return [
            {
                "sequence": 7,
                "id": "output-1",
                "command_id": "command-1",
                "request_id": "request-1",
                "conversation_id": "conversation-1",
                "modality": "text",
                "event_type": "text_delta",
                "payload": {"text": "Hola"},
                "user_id": "user-1",
                "tenant_id": "tenant-1",
            }
        ]


class FakeAcquire:
    """Return the shared fake connection from a pool acquisition."""

    def __init__(self, connection: FakeConnection) -> None:
        """Bind the connection returned by the context manager."""
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        """Acquire the fake connection."""
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        """Release the fake connection."""
        return None


class FakePool:
    """Expose the minimal asyncpg pool shape required by the adapter."""

    def __init__(self) -> None:
        """Create one reusable fake connection."""
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        """Acquire the reusable fake connection."""
        return FakeAcquire(self.connection)


def key() -> ConversationKey:
    """Build the full ownership key used in SQL adapter assertions."""
    return ConversationKey(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )


def repository(pool: FakePool) -> PostgresInteractionRepository:
    """Build the concrete adapter over the fake asyncpg pool."""
    return PostgresInteractionRepository(  # type: ignore[arg-type]
        pool,
        max_pending_commands=16,
    )


async def test_postgres_interactions_translate_commands_and_outputs() -> None:
    """Persist and rebuild neutral modalities without provider-specific fields."""
    pool = FakePool()
    store = repository(pool)
    command = InteractionCommand(
        command_id="command-1",
        request_id="request-1",
        conversation=key(),
        kind="user_message",
        source="speech_user",
        message="Transcripción",
    )
    await store.enqueue(command)
    claimed = await store.claim_next("coordinator-1", 150)
    output = InteractionOutput(
        output_id="output-1",
        command_id="command-1",
        request_id="request-1",
        conversation=key(),
        modality="text",
        event=AgentTextDelta(text="Hola"),
    )
    await store.append_output(output)
    loaded = await store.load_outputs(
        key(),
        after_sequence=0,
        command_id="command-1",
    )
    await store.acknowledge("output-1", key())
    await store.complete("command-1", "coordinator-1")

    assert claimed == InteractionCommand(
        command_id="command-1",
        request_id="request-1",
        conversation=key(),
        kind="user_message",
        source="speech_user",
        message="Transcripción",
        status="running",
        attempt_count=1,
    )
    assert loaded == (
        InteractionOutput(
            output_id="output-1",
            command_id="command-1",
            request_id="request-1",
            conversation=key(),
            modality="text",
            event=AgentTextDelta(text="Hola"),
            sequence=7,
        ),
    )
    assert (
        INSERT_COMMAND,
        (
            "command-1",
            "request-1",
            "conversation-1",
            "user_message",
            "speech_user",
            "Transcripción",
            None,
            16,
        ),
    ) in pool.connection.executed
    inserted = next(args for query, args in pool.connection.executed if query == INSERT_OUTPUT)
    assert inserted[:6] == (
        "output-1",
        "command-1",
        "request-1",
        "conversation-1",
        "text",
        "text_delta",
    )
    assert json.loads(str(inserted[6])) == {"text": "Hola"}
    assert (ACKNOWLEDGE_OUTPUT, ("output-1", "conversation-1", "user-1", "tenant-1")) in (
        pool.connection.executed
    )


async def test_postgres_interactions_reject_a_stale_completion() -> None:
    """Prevent a coordinator from closing a command after losing its lease."""
    pool = FakePool()
    pool.connection.update_status = "UPDATE 0"

    with pytest.raises(RuntimeError, match="claim was lost"):
        await repository(pool).complete("command-1", "stale-coordinator")

    assert (
        COMPLETE_COMMAND,
        ("command-1", "stale-coordinator"),
    ) in pool.connection.executed
