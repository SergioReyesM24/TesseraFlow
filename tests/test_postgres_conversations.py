import pytest

from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
)
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.tools import ToolCall, ToolResult
from infrastructure.postgres_conversations import (
    INSERT_CONVERSATION,
    INSERT_ITEM,
    SELECT_CONVERSATION,
    SELECT_CONVERSATION_FOR_UPDATE,
    SELECT_RECENT_ITEMS,
    UPDATE_CONVERSATION,
    PostgresConversationRepository,
    apply_postgres_migrations,
)


class FakeTransaction:
    """No-op async transaction context for repository unit tests."""

    async def __aenter__(self) -> "FakeTransaction":
        """Enter the transaction."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the transaction."""
        return None


class FakePostgresConnection:
    """Small stateful asyncpg connection double keyed by conversation ID."""

    def __init__(self) -> None:
        """Initialize canonical rows and ordered item records."""
        self.conversations: dict[str, dict[str, object]] = {}
        self.items: dict[str, list[dict[str, object]]] = {}

    def transaction(self) -> FakeTransaction:
        """Create a no-op transaction boundary."""
        return FakeTransaction()

    async def fetchrow(self, query: str, conversation_id: str) -> dict[str, object] | None:
        """Return one canonical metadata row."""
        assert query in (SELECT_CONVERSATION, SELECT_CONVERSATION_FOR_UPDATE)
        return self.conversations.get(conversation_id)

    async def fetch(self, query: str, conversation_id: str, limit: int) -> list[dict[str, object]]:
        """Return recent ordered item payloads."""
        assert query == SELECT_RECENT_ITEMS
        records = self.items.get(conversation_id, [])
        recent = records[-limit:]
        turn_ids = {record["turn_id"] for record in recent}
        return [
            {"payload": record["payload"]} for record in records if record["turn_id"] in turn_ids
        ]

    async def execute(self, query: str, *args: object) -> str:
        """Apply conversation metadata mutations."""
        if query == INSERT_CONVERSATION:
            conversation_id, user_id, title = args
            assert isinstance(conversation_id, str)
            self.conversations[conversation_id] = {
                "user_id": user_id,
                "title": title,
                "version": 0,
                "last_sequence": 0,
            }
            self.items[conversation_id] = []
        elif query == UPDATE_CONVERSATION:
            conversation_id, version, last_sequence, title = args
            assert isinstance(conversation_id, str)
            self.conversations[conversation_id]["version"] = version
            self.conversations[conversation_id]["last_sequence"] = last_sequence
            self.conversations[conversation_id]["title"] = title
        elif query == "DELETE FROM conversations WHERE id = $1":
            conversation_id = args[0]
            assert isinstance(conversation_id, str)
            self.conversations.pop(conversation_id, None)
            self.items.pop(conversation_id, None)
        else:
            raise AssertionError(f"Unexpected query: {query}")
        return "OK"

    async def executemany(self, query: str, records: list[tuple[object, ...]]) -> None:
        """Append ordered canonical conversation items."""
        assert query == INSERT_ITEM
        for record in records:
            conversation_id = record[0]
            assert isinstance(conversation_id, str)
            self.items[conversation_id].append(
                {
                    "turn_id": record[1],
                    "sequence": record[2],
                    "payload": record[7],
                }
            )


class FakeAcquire:
    """Async context returned by the fake pool acquire operation."""

    def __init__(self, connection: FakePostgresConnection) -> None:
        """Bind the shared fake connection."""
        self.connection = connection

    async def __aenter__(self) -> FakePostgresConnection:
        """Return the acquired fake connection."""
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        """Release the fake connection."""
        return None


class FakePostgresPool:
    """Pool double sharing one stateful connection."""

    def __init__(self) -> None:
        """Create the shared fake connection."""
        self.connection = FakePostgresConnection()

    def acquire(self) -> FakeAcquire:
        """Acquire the shared fake connection."""
        return FakeAcquire(self.connection)


def key(*, user_id: str = "user-1") -> ConversationKey:
    """Build one stable conversation ownership key."""
    return ConversationKey(conversation_id="conv-1", user_id=user_id)


def tool_turn(question: str = "Suma 2 y 3") -> tuple[ConversationItem, ...]:
    """Build one complete turn containing a matched tool call and result."""
    return (
        ConversationMessage(role="user", content=question),
        ToolCall(call_id="call-1", tool_name="calculator", arguments={"a": 2, "b": 3}),
        ToolResult(call_id="call-1", output={"result": 5}),
        ConversationMessage(role="assistant", content="El resultado es 5"),
    )


def repository(pool: FakePostgresPool) -> PostgresConversationRepository:
    """Construct the repository over a fake asyncpg pool."""
    return PostgresConversationRepository(pool, context_item_limit=100)  # type: ignore[arg-type]


async def test_postgres_appends_and_loads_complete_tool_turn() -> None:
    """Persist title, ownership, order, tool call, result, and assistant response."""
    pool = FakePostgresPool()
    value = Conversation(key=key())

    saved = await repository(pool).save_turn(value, tool_turn())
    loaded = await repository(pool).load(key())

    assert saved.version == 1
    assert saved.title == "Suma 2 y 3"
    assert loaded == saved
    assert [record["sequence"] for record in pool.connection.items["conv-1"]] == [1, 2, 3, 4]


async def test_postgres_creates_an_empty_session_before_its_first_turn() -> None:
    """Persist a version-zero conversation that can subsequently receive messages."""
    pool = FakePostgresPool()
    store = repository(pool)

    created = await store.create(key())
    loaded = await store.load(key())

    assert created == Conversation(key=key(), title="Nueva conversación")
    assert loaded == created
    assert pool.connection.items["conv-1"] == []


async def test_postgres_rejects_stale_versions_and_other_owners() -> None:
    """Enforce optimistic concurrency and canonical ownership checks."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.save_turn(Conversation(key=key()), tool_turn())

    with pytest.raises(ConversationConflictError):
        await store.save_turn(Conversation(key=key()), tool_turn("Otra"))
    with pytest.raises(ConversationAccessDeniedError):
        await store.load(key(user_id="user-2"))


async def test_postgres_delete_cascades_owned_history() -> None:
    """Delete conversation metadata and all associated item rows."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.save_turn(Conversation(key=key()), tool_turn())

    assert await store.delete(key()) is True
    assert await store.load(key()) is None
    assert pool.connection.items == {}


def test_postgres_rejects_unmatched_tool_history() -> None:
    """Reject incomplete turns before opening a database transaction."""
    invalid = (
        ConversationMessage(role="user", content="Suma"),
        ToolCall(call_id="call-1", tool_name="calculator", arguments={}),
        ConversationMessage(role="assistant", content="No sé"),
    )

    with pytest.raises(ValueError, match="matching result"):
        PostgresConversationRepository._validate_turn(invalid)


class FakeMigrationConnection:
    """Capture startup migration statements without contacting PostgreSQL."""

    def __init__(self) -> None:
        """Initialize executed statements and transaction boundaries."""
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        """Record one migration or bookkeeping statement."""
        self.statements.append((query, args))
        return "OK"

    async def fetch(self, query: str) -> list[dict[str, object]]:
        """Report that no migration has been applied yet."""
        assert query == "SELECT name FROM schema_migrations"
        return []

    def transaction(self) -> FakeTransaction:
        """Create a no-op migration transaction."""
        return FakeTransaction()


class FakeMigrationPool:
    """Expose one fake connection through the pool acquisition protocol."""

    def __init__(self) -> None:
        """Create the migration connection."""
        self.connection = FakeMigrationConnection()

    def acquire(self) -> FakeAcquire:
        """Acquire the migration connection."""
        return FakeAcquire(self.connection)  # type: ignore[arg-type]


async def test_postgres_migration_creates_metadata_and_item_tables() -> None:
    """Apply the bundled schema containing ownership metadata and ordered items."""
    pool = FakeMigrationPool()

    await apply_postgres_migrations(pool)  # type: ignore[arg-type]

    combined_sql = "\n".join(statement for statement, _ in pool.connection.statements)
    assert "CREATE TABLE IF NOT EXISTS conversations" in combined_sql
    assert "title TEXT" in combined_sql
    assert "metadata JSONB" in combined_sql
    assert "CREATE TABLE IF NOT EXISTS conversation_items" in combined_sql
    assert "REFERENCES conversations(id) ON DELETE CASCADE" in combined_sql
    assert "CREATE TABLE IF NOT EXISTS a2a_threads" in combined_sql
    assert "CREATE TABLE IF NOT EXISTS a2a_jobs" in combined_sql
    assert "CREATE TRIGGER interaction_command_notify_trigger" in combined_sql
    assert "CREATE TRIGGER interaction_output_notify_trigger" in combined_sql
    assert "CREATE TRIGGER a2a_job_notify_trigger" in combined_sql
    assert "'queued', 'completed', 'failed', 'cancelled'" in combined_sql
    assert "'audio_delta'" in combined_sql
    assert "'audio_interrupted'" in combined_sql
    assert any(args == ("001_conversations.sql",) for _, args in pool.connection.statements)
    assert any(args == ("002_a2a_jobs.sql",) for _, args in pool.connection.statements)
    assert any(
        args == ("004_interaction_notifications.sql",) for _, args in pool.connection.statements
    )
    assert any(
        args == ("005_interaction_audio_events.sql",) for _, args in pool.connection.statements
    )
    assert any(args == ("006_a2a_job_notifications.sql",) for _, args in pool.connection.statements)
