import pytest

from domain.a2a import A2AJob, A2AThread
from domain.conversations import ConversationKey
from infrastructure.postgres_a2a import (
    CLAIM_NEXT_JOB,
    COMPLETE_JOB,
    INSERT_JOB,
    INSERT_THREAD,
    SELECT_JOB,
    SELECT_THREAD,
    SELECT_THREAD_FOR_JOB,
    PostgresA2AJobRepository,
)


class FakeTransaction:
    """Provide a no-op async transaction for repository tests."""

    async def __aenter__(self) -> "FakeTransaction":
        """Enter the fake transaction."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the fake transaction."""
        return None


class FakeConnection:
    """Capture A2A SQL and return deterministic joined rows."""

    def __init__(self) -> None:
        """Initialize statement observations and mutation results."""
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.update_status = "UPDATE 1"
        self.thread_row = {
            "id": "thread-1",
            "parent_conversation_id": "parent-1",
            "worker_conversation_id": "worker-conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
        }
        self.job_row = {
            "id": "job-1",
            "thread_id": "thread-1",
            "message": "Investiga",
            "status": "queued",
            "answer": None,
            "response_id": None,
            "error_code": None,
            **{key: value for key, value in self.thread_row.items() if key != "id"},
        }

    def transaction(self) -> FakeTransaction:
        """Open a no-op transaction."""
        return FakeTransaction()

    async def execute(self, query: str, *args: object) -> str:
        """Record a write and return its configured asyncpg status."""
        self.executed.append((query, args))
        return self.update_status

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        """Return the row shape expected by each repository query."""
        self.executed.append((query, args))
        if query == SELECT_THREAD:
            return self.thread_row
        if query == SELECT_JOB:
            return self.job_row
        if query == CLAIM_NEXT_JOB:
            return {
                key: value
                for key, value in self.job_row.items()
                if key
                in {
                    "id",
                    "thread_id",
                    "message",
                    "status",
                    "answer",
                    "response_id",
                    "error_code",
                }
            } | {"status": "running"}
        if query == SELECT_THREAD_FOR_JOB:
            return {key: value for key, value in self.thread_row.items() if key != "id"}
        if query == COMPLETE_JOB:
            return {"id": "a2a-result:job-1"} if self.update_status == "UPDATE 1" else None
        raise AssertionError(f"Unexpected query: {query}")


class FakeAcquire:
    """Return the shared fake connection from an async context."""

    def __init__(self, connection: FakeConnection) -> None:
        """Bind the connection returned on entry."""
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        """Acquire the fake connection."""
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        """Release the fake connection."""
        return None


class FakePool:
    """Expose one connection with the asyncpg pool acquisition shape."""

    def __init__(self) -> None:
        """Create the shared fake connection."""
        self.connection = FakeConnection()

    def acquire(self) -> FakeAcquire:
        """Acquire the shared fake connection."""
        return FakeAcquire(self.connection)


def parent_key() -> ConversationKey:
    """Build the parent conversation used by repository ownership checks."""
    return ConversationKey(
        conversation_id="parent-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )


def repository(pool: FakePool) -> PostgresA2AJobRepository:
    """Build the adapter over a fake pool."""
    return PostgresA2AJobRepository(pool)  # type: ignore[arg-type]


async def test_postgres_a2a_creates_and_loads_owned_protocol_state() -> None:
    """Translate threads and jobs in both directions without provider details."""
    pool = FakePool()
    store = repository(pool)
    thread = A2AThread(
        thread_id="thread-1",
        parent_conversation=parent_key(),
        worker_conversation_id="worker-conversation-1",
    )
    job = A2AJob(
        job_id="job-1",
        thread_id="thread-1",
        parent_conversation=parent_key(),
        worker_conversation_id="worker-conversation-1",
        message="Investiga",
    )

    await store.create_thread(thread, job)
    loaded_thread = await store.load_thread("thread-1", parent_key())
    loaded_job = await store.load_job("job-1", parent_key())

    assert loaded_thread == thread
    assert loaded_job == job
    assert (
        INSERT_THREAD,
        ("thread-1", "parent-1", "worker-conversation-1", "user-1", "tenant-1"),
    ) in pool.connection.executed
    assert (INSERT_JOB, ("job-1", "thread-1", "Investiga")) in pool.connection.executed


async def test_postgres_a2a_claim_combines_job_and_thread_without_losing_ids() -> None:
    """Lease one job and preserve both its public job and thread identifiers."""
    pool = FakePool()

    claimed = await repository(pool).claim_next("process-1", 630)

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.thread_id == "thread-1"
    assert claimed.status == "running"
    assert claimed.worker_conversation_id == "worker-conversation-1"
    assert (CLAIM_NEXT_JOB, ("process-1", 630)) in pool.connection.executed


async def test_postgres_a2a_rejects_completion_after_a_claim_is_lost() -> None:
    """Prevent a stale worker from overwriting a job reclaimed by another process."""
    pool = FakePool()
    pool.connection.update_status = "UPDATE 0"

    with pytest.raises(RuntimeError, match="claim was lost"):
        await repository(pool).complete(
            "job-1",
            "old-process",
            "Respuesta",
            "resp-1",
            "notification",
        )

    assert (
        COMPLETE_JOB,
        ("job-1", "old-process", "Respuesta", "resp-1", "notification"),
    ) in pool.connection.executed


async def test_postgres_a2a_publishes_completion_with_the_job_update() -> None:
    """Use one SQL statement for terminal state and parent-conversation wake-up."""
    pool = FakePool()

    await repository(pool).complete(
        "job-1",
        "process-1",
        "Respuesta",
        "resp-1",
        "notification",
    )

    assert (
        COMPLETE_JOB,
        ("job-1", "process-1", "Respuesta", "resp-1", "notification"),
    ) in pool.connection.executed
