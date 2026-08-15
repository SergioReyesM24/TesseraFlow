from datetime import UTC, datetime
from decimal import Decimal

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
from domain.costs import ModelCost, ModelUsage
from domain.evaluations import EvaluationTrace
from domain.tools import ToolCall, ToolResult
from infrastructure.postgres_conversations import (
    INSERT_CONVERSATION,
    INSERT_EVALUATION_TRACE,
    INSERT_ITEM,
    SELECT_CONVERSATION,
    SELECT_CONVERSATION_FOR_UPDATE,
    SELECT_CONVERSATION_GROUP,
    SELECT_CONVERSATION_HISTORY,
    SELECT_CONVERSATION_SUMMARIES,
    SELECT_DAILY_TOKEN_USAGE,
    SELECT_EVALUATION_TRACES,
    SELECT_HISTORY_ITEMS,
    SELECT_RECENT_ITEMS,
    SELECT_SESSION_TOKEN_USAGE,
    UPDATE_CONVERSATION,
    InvalidPostgresConversationDataError,
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
        self.group_rows: dict[str, list[dict[str, object]]] = {}
        self.daily_usage_rows: list[dict[str, object]] = []
        self.session_usage_rows: dict[str, list[dict[str, object]]] = {}
        self.evaluation_traces: dict[str, list[dict[str, object]]] = {}

    def transaction(self) -> FakeTransaction:
        """Create a no-op transaction boundary."""
        return FakeTransaction()

    async def fetchrow(self, query: str, conversation_id: str) -> dict[str, object] | None:
        """Return one canonical metadata row."""
        assert query in (
            SELECT_CONVERSATION,
            SELECT_CONVERSATION_FOR_UPDATE,
            SELECT_CONVERSATION_HISTORY,
        )
        return self.conversations.get(conversation_id)

    async def fetch(self, query: str, conversation_id: str, *args: int) -> list[dict[str, object]]:
        """Return compacted context or a bounded technical history page."""
        if query == SELECT_CONVERSATION_GROUP:
            return self.group_rows.get(conversation_id, [])
        if query == SELECT_DAILY_TOKEN_USAGE:
            return self.daily_usage_rows
        if query == SELECT_SESSION_TOKEN_USAGE:
            return self.session_usage_rows.get(conversation_id, [])
        if query == SELECT_CONVERSATION_SUMMARIES:
            offset, limit = args
            rows = [
                {"id": item_id, **row}
                for item_id, row in self.conversations.items()
                if row["user_id"] == conversation_id and bool(self.items.get(item_id))
            ]
            rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            total = len(rows)
            page = rows[offset : offset + limit]
            if not page:
                return [{"id": None, "total_count": total}]
            return [{**row, "total_count": total} for row in page]
        if query == SELECT_EVALUATION_TRACES:
            (limit,) = args
            return self.evaluation_traces.get(conversation_id, [])[:limit]
        records = self.items.get(conversation_id, [])
        if query == SELECT_HISTORY_ITEMS:
            after_sequence, limit = args
            return [record for record in records if int(record["sequence"]) > after_sequence][
                :limit
            ]
        assert query == SELECT_RECENT_ITEMS
        (limit,) = args
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
                "status": "active",
                "version": 0,
                "last_sequence": 0,
                "created_at": datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
                "updated_at": datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
                "last_message_at": None,
                "parent_conversation_id": None,
                "worker_conversation_id": None,
                "thread_id": None,
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
            self.evaluation_traces.pop(conversation_id, None)
        elif query == INSERT_EVALUATION_TRACE:
            (
                trace_id,
                conversation_id,
                turn_id,
                job_id,
                attempt,
                proposed_call_ids,
                verdict,
                risk,
                reason_code,
                feedback,
                mode,
                executed,
                model,
                usage,
                latency_ms,
                created_at,
            ) = args
            assert isinstance(conversation_id, str)
            self.evaluation_traces.setdefault(conversation_id, []).append(
                {
                    "id": trace_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "job_id": job_id,
                    "attempt": attempt,
                    "proposed_call_ids": proposed_call_ids,
                    "verdict": verdict,
                    "risk": risk,
                    "reason_code": reason_code,
                    "feedback": feedback,
                    "mode": mode,
                    "executed": executed,
                    "model": model,
                    "usage": usage,
                    "latency_ms": latency_ms,
                    "created_at": created_at,
                }
            )
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
                    "created_at": datetime(2026, 7, 22, 10, 1, tzinfo=UTC),
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


def key(
    *,
    conversation_id: str = "conv-1",
    user_id: str = "user-1",
) -> ConversationKey:
    """Build one stable conversation ownership key."""
    return ConversationKey(conversation_id=conversation_id, user_id=user_id)


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

    saved = await repository(pool).save_turn(
        value,
        tool_turn(),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    loaded = await repository(pool).load(key())

    assert saved.version == 1
    assert saved.title == "Suma 2 y 3"
    assert loaded == saved
    assert [record["sequence"] for record in pool.connection.items["conv-1"]] == [1, 2, 3, 4]


async def test_postgres_loads_paginated_technical_history_with_tool_records() -> None:
    """Expose canonical row metadata and preserve call/result correlation."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.save_turn(
        Conversation(key=key()),
        tool_turn(),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    first = await store.load_history(key(), after_sequence=0, limit=2)
    second = await store.load_history(key(), after_sequence=2, limit=2)

    assert first is not None
    assert first.version == 1
    assert first.last_sequence == 4
    assert first.has_more is True
    assert [record.sequence for record in first.items] == [1, 2]
    assert isinstance(first.items[1].item, ToolCall)
    assert second is not None
    assert second.has_more is False
    assert isinstance(second.items[0].item, ToolResult)


async def test_postgres_persists_evaluation_traces_outside_conversation_items() -> None:
    pool = FakePostgresPool()
    store = repository(pool)
    await store.create(key())
    created_at = datetime(2026, 7, 22, 10, 1, tzinfo=UTC)
    trace = EvaluationTrace(
        trace_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        conversation_id="conv-1",
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        job_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        attempt=1,
        proposed_call_ids=("call-1",),
        verdict="fail",
        risk="low",
        reason_code="wrong_tool",
        feedback="Selecciona una herramienta adecuada.",
        mode="enforce",
        executed=False,
        model="evaluation-model",
        usage=ModelUsage(input_tokens=100, output_tokens=20),
        latency_ms=125.5,
        created_at=created_at,
    )

    await store.append_evaluation_trace(trace)
    history = await store.load_history(key(), after_sequence=0, limit=50)

    assert history is not None
    assert history.evaluations.traces == (trace,)
    assert history.items == ()


async def test_postgres_history_enforces_conversation_ownership() -> None:
    """Reject technical inspection through another user identifier."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.create(key())

    with pytest.raises(ConversationAccessDeniedError):
        await store.load_history(key(user_id="user-2"), after_sequence=0, limit=50)


async def test_postgres_creates_an_empty_session_before_its_first_turn() -> None:
    """Persist a version-zero conversation that can subsequently receive messages."""
    pool = FakePostgresPool()
    store = repository(pool)

    created = await store.create(key())
    loaded = await store.load(key())

    assert created == Conversation(key=key(), title="Nueva conversación")
    assert loaded == created
    assert pool.connection.items["conv-1"] == []


async def test_postgres_lists_only_the_users_sessions_with_pagination() -> None:
    """Exclude empty sessions before paginating the user's conversation headers."""
    pool = FakePostgresPool()
    store = repository(pool)
    first_conversation = await store.create(key())
    await store.save_turn(
        first_conversation,
        tool_turn("Primera conversación"),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    await store.create(key(conversation_id="conv-empty"))
    second_conversation = await store.create(key(conversation_id="conv-2"))
    await store.save_turn(
        second_conversation,
        tool_turn("Segunda conversación"),
        turn_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    await store.create(key(conversation_id="foreign", user_id="user-2"))

    first = await store.list_sessions("user-1", offset=0, limit=1)
    second = await store.list_sessions("user-1", offset=1, limit=1)
    beyond_last = await store.list_sessions("user-1", offset=10, limit=1)

    assert [item.key.conversation_id for item in first.sessions] == ["conv-2"]
    assert first.total == 2
    assert first.has_more is True
    assert [item.key.conversation_id for item in second.sessions] == ["conv-1"]
    assert second.total == 2
    assert second.has_more is False
    assert beyond_last.sessions == ()
    assert beyond_last.total == 2
    assert beyond_last.has_more is False
    assert "FROM conversation_items AS item" in SELECT_CONVERSATION_SUMMARIES
    assert "item.item_type = 'message'" in SELECT_CONVERSATION_SUMMARIES
    assert "NOT EXISTS" in SELECT_CONVERSATION_SUMMARIES


async def test_postgres_maps_global_daily_token_usage() -> None:
    """Return normalized zero-safe usage rows across all owner conversations."""
    pool = FakePostgresPool()
    pool.connection.daily_usage_rows = [
        {
            "usage_date": "21-07-2026",
            "input_tokens": 1_000,
            "output_tokens": 200,
            "cached_input_tokens": 400,
            "cached_input_audio_tokens": 10,
            "reasoning_tokens": 50,
            "input_audio_tokens": 100,
            "output_audio_tokens": 20,
            "cost_amount": "0.0012",
            "cost_currency": "USD",
            "fully_priced": True,
            "turn_count": 3,
            "model_call_count": 5,
        }
    ]

    usage = await repository(pool).load_daily_token_usage("user-1", days=30)

    assert len(usage) == 1
    assert usage[0].usage.input_tokens == 1_000
    assert usage[0].usage.output_tokens == 200
    assert usage[0].usage.cached_input_tokens == 400
    assert usage[0].day == "21-07-2026"
    assert usage[0].cost is not None
    assert usage[0].cost.amount == Decimal("0.0012")
    assert usage[0].cost.currency == "USD"
    assert usage[0].fully_priced is True
    assert usage[0].turn_count == 3
    assert usage[0].model_call_count == 5
    assert "conversation.user_id = $1" in SELECT_DAILY_TOKEN_USAGE
    assert "jsonb_array_elements" in SELECT_DAILY_TOKEN_USAGE
    assert "model_call.value #>> '{usage,input_tokens}'" in SELECT_DAILY_TOKEN_USAGE
    assert "model_call.value #>> '{cost,amount}'" in SELECT_DAILY_TOKEN_USAGE


async def test_postgres_aggregates_session_usage_without_merging_member_histories() -> None:
    """Map root, worker, model, turn, and exact cost totals from the SQL projection."""
    pool = FakePostgresPool()
    pool.connection.session_usage_rows["root-1"] = [
        usage_row(
            conversation_id="root-1",
            title="Principal",
            thread_id=None,
            item_id=11,
            model="model-a",
            input_tokens=100,
            output_tokens=20,
            cost_amount="0.001",
        ),
        usage_row(
            conversation_id="root-1",
            title="Principal",
            thread_id=None,
            item_id=11,
            model="model-b",
            input_tokens=50,
            output_tokens=10,
            cost_amount="0.002",
        ),
        usage_row(
            conversation_id="worker-1",
            title="Worker",
            thread_id="thread-1",
            item_id=21,
            model="model-a",
            input_tokens=200,
            output_tokens=40,
            cost_amount="0.003",
        ),
    ]

    report = await repository(pool).load_session_token_usage(key(conversation_id="root-1"))

    assert report is not None
    assert report.usage.total_tokens == 420
    assert report.turn_count == 2
    assert report.model_call_count == 3
    assert report.cost == ModelCost(amount=Decimal("0.006"), currency="USD")
    assert [item.conversation_id for item in report.conversations] == ["root-1", "worker-1"]
    assert report.conversations[0].turn_count == 1
    assert report.conversations[0].model_call_count == 2
    assert report.conversations[1].role == "worker"
    assert [(item.model, item.model_call_count) for item in report.models] == [
        ("model-a", 2),
        ("model-b", 1),
    ]
    assert "member.user_id = requested.user_id" in SELECT_SESSION_TOKEN_USAGE


async def test_postgres_marks_session_cost_unknown_when_any_call_is_unpriced() -> None:
    """Never expose a partial amount as though it were the complete session cost."""
    pool = FakePostgresPool()
    pool.connection.session_usage_rows["root-1"] = [
        usage_row(
            conversation_id="root-1",
            title="Principal",
            thread_id=None,
            item_id=11,
            model="model-a",
            input_tokens=100,
            output_tokens=20,
            cost_amount=None,
        )
    ]

    report = await repository(pool).load_session_token_usage(key(conversation_id="root-1"))

    assert report is not None
    assert report.cost is None
    assert report.fully_priced is False
    assert report.models[0].cost is None
    assert report.models[0].fully_priced is False


async def test_postgres_session_usage_enforces_requested_ownership() -> None:
    """Reject usage reads when the requested conversation belongs to another owner."""
    pool = FakePostgresPool()
    pool.connection.session_usage_rows["root-1"] = [
        usage_row(
            conversation_id="root-1",
            title="Principal",
            thread_id=None,
            item_id=None,
            model=None,
            input_tokens=0,
            output_tokens=0,
            cost_amount=None,
        )
    ]

    with pytest.raises(ConversationAccessDeniedError):
        await repository(pool).load_session_token_usage(
            key(conversation_id="root-1", user_id="user-2")
        )


def usage_row(
    *,
    conversation_id: str,
    title: str,
    thread_id: str | None,
    item_id: int | None,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_amount: str | None,
) -> dict[str, object]:
    """Build one stable row from the session-usage SQL projection."""
    return {
        "requested_user_id": "user-1",
        "root_id": "root-1",
        "conversation_id": conversation_id,
        "title": title,
        "thread_id": thread_id,
        "item_id": item_id,
        "turn_id": "turn-1" if item_id is not None else None,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": 0,
        "cached_input_audio_tokens": 0,
        "reasoning_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
        "cost_amount": cost_amount,
        "cost_currency": "USD" if cost_amount is not None else None,
    }


async def test_postgres_groups_multiple_worker_threads_without_merging_histories() -> None:
    """Project several worker sessions from either member through one root relation."""
    pool = FakePostgresPool()
    rows = [
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "root-1",
            "parent_conversation_id": None,
            "worker_conversation_id": None,
            "thread_id": None,
            "job_id": None,
            "job_status": None,
        },
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "worker-1",
            "parent_conversation_id": "root-1",
            "worker_conversation_id": "worker-1",
            "thread_id": "thread-1",
            "job_id": "job-1",
            "job_status": "completed",
        },
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "worker-1",
            "parent_conversation_id": "root-1",
            "worker_conversation_id": "worker-1",
            "thread_id": "thread-1",
            "job_id": "job-2",
            "job_status": "queued",
        },
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "worker-2",
            "parent_conversation_id": "root-1",
            "worker_conversation_id": "worker-2",
            "thread_id": "thread-2",
            "job_id": "job-3",
            "job_status": "running",
        },
    ]
    pool.connection.group_rows["worker-2"] = rows
    store = repository(pool)

    group = await store.load_group(ConversationKey(conversation_id="worker-2", user_id="user-1"))

    assert group is not None
    assert group.root_conversation.conversation_id == "root-1"
    assert [member.correlation.conversation_id for member in group.members] == [
        "root-1",
        "worker-1",
        "worker-2",
    ]
    assert [job.job_id for job in group.members[1].jobs] == ["job-1", "job-2"]
    assert group.members[1].jobs[0].request_id == "job-1"
    assert group.members[1].jobs[0].turn_id == "job-1"

    with pytest.raises(ConversationAccessDeniedError):
        await store.load_group(ConversationKey(conversation_id="worker-2", user_id="user-2"))


async def test_postgres_rejects_an_ambiguous_worker_projection() -> None:
    """Fail closed if storage ever associates one worker session with two threads."""
    pool = FakePostgresPool()
    pool.connection.group_rows["worker-1"] = [
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "root-1",
            "parent_conversation_id": None,
            "worker_conversation_id": None,
            "thread_id": None,
            "job_id": None,
            "job_status": None,
        },
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "worker-1",
            "parent_conversation_id": "root-1",
            "worker_conversation_id": "worker-1",
            "thread_id": "thread-1",
            "job_id": None,
            "job_status": None,
        },
        {
            "requested_user_id": "user-1",
            "root_id": "root-1",
            "conversation_id": "worker-1",
            "parent_conversation_id": "root-1",
            "worker_conversation_id": "worker-1",
            "thread_id": "thread-2",
            "job_id": None,
            "job_status": None,
        },
    ]

    with pytest.raises(
        InvalidPostgresConversationDataError,
        match="correlation data is invalid",
    ):
        await repository(pool).load_group(
            ConversationKey(conversation_id="worker-1", user_id="user-1")
        )


async def test_postgres_rejects_stale_versions_and_other_owners() -> None:
    """Enforce optimistic concurrency and canonical ownership checks."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.save_turn(
        Conversation(key=key()),
        tool_turn(),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    with pytest.raises(ConversationConflictError):
        await store.save_turn(
            Conversation(key=key()),
            tool_turn("Otra"),
            turn_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
    with pytest.raises(ConversationAccessDeniedError):
        await store.load(key(user_id="user-2"))


async def test_postgres_delete_cascades_owned_history() -> None:
    """Delete conversation metadata and all associated item rows."""
    pool = FakePostgresPool()
    store = repository(pool)
    await store.save_turn(
        Conversation(key=key()),
        tool_turn(),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

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
    assert "CREATE TABLE IF NOT EXISTS evaluation_traces" in combined_sql
    assert "CREATE TRIGGER interaction_command_notify_trigger" in combined_sql
    assert "CREATE TRIGGER interaction_output_notify_trigger" in combined_sql
    assert "CREATE TRIGGER a2a_job_notify_trigger" in combined_sql
    assert "'queued', 'completed', 'failed', 'cancelled'" in combined_sql
    assert "'audio_delta'" in combined_sql
    assert "'audio_interrupted'" in combined_sql
    assert "a2a_jobs_delivery_mode_check" in combined_sql
    assert "interaction_commands_delivery_mode_check" in combined_sql
    assert "interaction_commands_realtime_claim_idx" in combined_sql
    assert "ADD COLUMN IF NOT EXISTS user_id TEXT" in combined_sql
    assert "SET user_id = parent.user_id" in combined_sql
    assert "DROP CONSTRAINT IF EXISTS a2a_threads_distinct_conversations_check" in combined_sql
    assert "validate_a2a_conversation_correlation" in combined_sql
    assert "NEW.user_id <> parent_user_id" in combined_sql
    assert "conversation_items_assistant_usage_idx" in combined_sql
    assert any(args == ("001_conversations.sql",) for _, args in pool.connection.statements)
    assert any(args == ("002_a2a_jobs.sql",) for _, args in pool.connection.statements)
    assert any(
        args == ("008_a2a_conversation_correlation.sql",) for _, args in pool.connection.statements
    )
    assert any(
        args == ("004_interaction_notifications.sql",) for _, args in pool.connection.statements
    )
    assert any(
        args == ("005_interaction_audio_events.sql",) for _, args in pool.connection.statements
    )
    assert any(args == ("006_a2a_job_notifications.sql",) for _, args in pool.connection.statements)
    assert any(
        args == ("007_interaction_delivery_modes.sql",) for _, args in pool.connection.statements
    )
