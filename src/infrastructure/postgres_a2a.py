from typing import Any, cast

import asyncpg

from application.ports import A2AJobRepository
from domain.a2a import A2AJob, A2AJobStatus, A2AThread
from domain.conversations import ConversationKey

INSERT_THREAD = """
INSERT INTO a2a_threads (
    id, parent_conversation_id, worker_conversation_id, user_id
) VALUES ($1, $2, $3, $4)
"""

INSERT_JOB = """
INSERT INTO a2a_jobs (id, thread_id, message)
VALUES ($1, $2, $3)
"""

SELECT_THREAD = """
SELECT id, parent_conversation_id, worker_conversation_id, user_id
FROM a2a_threads
WHERE id = $1
  AND parent_conversation_id = $2
  AND user_id = $3
"""

SELECT_JOB = """
SELECT
    j.id,
    j.thread_id,
    j.message,
    j.status,
    j.answer,
    j.response_id,
    j.error_code,
    t.parent_conversation_id,
    t.worker_conversation_id,
    t.user_id
FROM a2a_jobs AS j
JOIN a2a_threads AS t ON t.id = j.thread_id
WHERE j.id = $1
  AND t.parent_conversation_id = $2
  AND t.user_id = $3
"""

CLAIM_NEXT_JOB = """
WITH candidate AS (
    SELECT job.id
    FROM a2a_jobs AS job
    WHERE (
        job.status = 'queued'
        OR (job.status = 'running' AND job.lease_expires_at < NOW())
    )
      AND NOT EXISTS (
          SELECT 1
          FROM a2a_jobs AS earlier
          WHERE earlier.thread_id = job.thread_id
            AND earlier.sequence < job.sequence
            AND earlier.status IN ('queued', 'running')
    )
    ORDER BY job.sequence
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE a2a_jobs AS job
SET status = 'running',
    worker_id = $1,
    lease_expires_at = NOW() + ($2 * INTERVAL '1 second'),
    attempt_count = job.attempt_count + 1,
    started_at = COALESCE(job.started_at, NOW()),
    completed_at = NULL,
    error_code = NULL
FROM candidate
WHERE job.id = candidate.id
RETURNING job.id, job.thread_id, job.message, job.status, job.answer,
          job.response_id, job.error_code
"""

COMPLETE_JOB = """
WITH completed AS (
    UPDATE a2a_jobs
    SET status = 'completed',
        answer = $3,
        response_id = $4,
        error_code = NULL,
        worker_id = NULL,
        lease_expires_at = NULL,
        completed_at = NOW()
    WHERE id = $1 AND worker_id = $2 AND status = 'running'
    RETURNING id, thread_id
)
INSERT INTO interaction_commands (
    id, request_id, conversation_id, kind, source, message, causation_id
)
SELECT 'a2a-result:' || completed.id,
       completed.id,
       thread.parent_conversation_id,
       'worker_completed',
       'worker_agent',
       $5,
       completed.id
FROM completed
JOIN a2a_threads AS thread ON thread.id = completed.thread_id
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

FAIL_JOB = """
WITH failed AS (
    UPDATE a2a_jobs
    SET status = 'failed',
        answer = NULL,
        response_id = NULL,
        error_code = $3,
        worker_id = NULL,
        lease_expires_at = NULL,
        completed_at = NOW()
    WHERE id = $1 AND worker_id = $2 AND status = 'running'
    RETURNING id, thread_id
)
INSERT INTO interaction_commands (
    id, request_id, conversation_id, kind, source, message, causation_id
)
SELECT 'a2a-result:' || failed.id,
       failed.id,
       thread.parent_conversation_id,
       'worker_completed',
       'worker_agent',
       $4,
       failed.id
FROM failed
JOIN a2a_threads AS thread ON thread.id = failed.thread_id
ON CONFLICT (id) DO NOTHING
RETURNING id
"""

REQUEUE_JOB = """
UPDATE a2a_jobs
SET status = 'queued',
    worker_id = NULL,
    lease_expires_at = NULL,
    completed_at = NULL
WHERE id = $1 AND worker_id = $2 AND status = 'running'
"""

SELECT_THREAD_FOR_JOB = """
SELECT parent_conversation_id, worker_conversation_id, user_id
FROM a2a_threads
WHERE id = $1
"""


class PostgresA2AJobRepository(A2AJobRepository):
    """Durable PostgreSQL queue that serializes messages within each A2A thread."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind the shared process-level PostgreSQL pool."""
        self._pool = pool

    async def create_thread(self, thread: A2AThread, first_job: A2AJob) -> None:
        """Insert the thread and first message in one transaction."""
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                INSERT_THREAD,
                thread.thread_id,
                thread.parent_conversation.conversation_id,
                thread.worker_conversation_id,
                thread.parent_conversation.user_id,
            )
            await connection.execute(
                INSERT_JOB,
                first_job.job_id,
                first_job.thread_id,
                first_job.message,
            )

    async def load_thread(
        self,
        thread_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AThread | None:
        """Read one thread through its complete ownership boundary."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                SELECT_THREAD,
                thread_id,
                parent_conversation.conversation_id,
                parent_conversation.user_id,
            )
        return self._thread_from_row(row) if row is not None else None

    async def enqueue(self, job: A2AJob) -> None:
        """Append one durable message to the worker thread."""
        async with self._pool.acquire() as connection:
            await connection.execute(INSERT_JOB, job.job_id, job.thread_id, job.message)

    async def load_job(
        self,
        job_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AJob | None:
        """Read a job only through the parent conversation that created it."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                SELECT_JOB,
                job_id,
                parent_conversation.conversation_id,
                parent_conversation.user_id,
            )
        return self._job_from_row(row) if row is not None else None

    async def claim_next(self, worker_id: str, lease_seconds: float) -> A2AJob | None:
        """Atomically lease the first runnable message across all worker processes."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(CLAIM_NEXT_JOB, worker_id, lease_seconds)
            if row is None:
                return None
            thread_row = await connection.fetchrow(SELECT_THREAD_FOR_JOB, row["thread_id"])
        if thread_row is None:
            raise RuntimeError("Claimed A2A job has no thread")
        return self._job_from_row({**dict(row), **dict(thread_row)})

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        answer: str,
        response_id: str,
        notification_message: str,
    ) -> None:
        """Commit success and its parent notification in one SQL statement."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                COMPLETE_JOB,
                job_id,
                worker_id,
                answer,
                response_id,
                notification_message,
            )
        self._require_published(row, job_id)

    async def fail(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        notification_message: str,
    ) -> None:
        """Commit failure and its parent notification in one SQL statement."""
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                FAIL_JOB,
                job_id,
                worker_id,
                error_code,
                notification_message,
            )
        self._require_published(row, job_id)

    async def requeue(self, job_id: str, worker_id: str) -> None:
        """Make an interrupted job immediately claimable by another worker."""
        async with self._pool.acquire() as connection:
            await connection.execute(REQUEUE_JOB, job_id, worker_id)

    @staticmethod
    def _thread_from_row(row: Any) -> A2AThread:
        """Decode one database row into the provider-neutral thread aggregate."""
        return A2AThread(
            thread_id=str(row["id"]),
            parent_conversation=ConversationKey(
                conversation_id=str(row["parent_conversation_id"]),
                user_id=str(row["user_id"]),
            ),
            worker_conversation_id=str(row["worker_conversation_id"]),
        )

    @staticmethod
    def _job_from_row(row: Any) -> A2AJob:
        """Decode one joined job row without exposing queue implementation details."""
        return A2AJob(
            job_id=str(row["id"]),
            thread_id=str(row["thread_id"]),
            parent_conversation=ConversationKey(
                conversation_id=str(row["parent_conversation_id"]),
                user_id=str(row["user_id"]),
            ),
            worker_conversation_id=str(row["worker_conversation_id"]),
            message=str(row["message"]),
            status=cast(A2AJobStatus, str(row["status"])),
            answer=str(row["answer"]) if row["answer"] is not None else None,
            response_id=str(row["response_id"]) if row["response_id"] is not None else None,
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
        )

    @staticmethod
    def _require_updated(status: str, job_id: str) -> None:
        """Reject stale workers whose lease was already reclaimed."""
        if status != "UPDATE 1":
            raise RuntimeError(f"A2A job claim was lost: {job_id}")

    @staticmethod
    def _require_published(row: Any, job_id: str) -> None:
        """Reject stale workers when no terminal command could be published."""
        if row is None:
            raise RuntimeError(f"A2A job claim was lost: {job_id}")
