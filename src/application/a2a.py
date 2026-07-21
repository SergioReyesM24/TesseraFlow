import asyncio
import uuid
from collections.abc import Callable

import structlog

from application.agent import AgentService
from application.ports import A2AJobRepository, ConversationRepository
from domain.a2a import (
    A2ACompletionMessage,
    A2AJob,
    A2AJobReceipt,
    A2AJobReport,
    A2AMessage,
    A2AThread,
)
from domain.agent import AgentDefinition
from domain.conversations import ConversationKey, ConversationMessage

logger = structlog.get_logger(__name__)


class A2AThreadNotFoundError(LookupError):
    """Raised when an A2A thread is unknown or outside the parent conversation."""


class A2AJobNotFoundError(LookupError):
    """Raised when an A2A job is unknown or outside the parent conversation."""


class A2AService:
    """Create, continue, and inspect durable conversations with a worker agent."""

    def __init__(
        self,
        jobs: A2AJobRepository,
        conversations: ConversationRepository,
        *,
        uid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        """Bind durable stores and an injectable public identifier factory."""
        self._jobs = jobs
        self._conversations = conversations
        self._uid_factory = uid_factory

    async def delegate(self, parent: ConversationKey, message: str) -> A2AJobReceipt:
        """Start a worker conversation and enqueue the interactive agent's message."""
        thread_id = str(self._uid_factory())
        job_id = str(self._uid_factory())
        worker_key = ConversationKey(
            conversation_id=str(self._uid_factory()),
            user_id=parent.user_id,
            tenant_id=parent.tenant_id,
        )
        thread = A2AThread(
            thread_id=thread_id,
            parent_conversation=parent,
            worker_conversation_id=worker_key.conversation_id,
        )
        job = A2AJob(
            job_id=job_id,
            thread_id=thread_id,
            parent_conversation=parent,
            worker_conversation_id=worker_key.conversation_id,
            message=message,
        )
        await self._conversations.create(worker_key)
        try:
            await self._jobs.create_thread(thread, job)
        except BaseException:
            try:
                await asyncio.shield(self._conversations.delete(worker_key))
            except Exception as cleanup_exc:
                logger.warning(
                    "a2a_thread_cleanup_failed",
                    error_type=type(cleanup_exc).__name__,
                )
            raise
        logger.info("a2a_thread_created", thread_id=thread_id, job_id=job_id)
        return A2AJobReceipt(thread_id=thread_id, job_id=job_id, status="queued")

    async def continue_thread(
        self,
        parent: ConversationKey,
        thread_id: str,
        message: str,
    ) -> A2AJobReceipt:
        """Append a human-style follow-up to an existing worker-agent history."""
        thread = await self._jobs.load_thread(thread_id, parent)
        if thread is None:
            raise A2AThreadNotFoundError("Worker thread does not exist")
        job = A2AJob(
            job_id=str(self._uid_factory()),
            thread_id=thread.thread_id,
            parent_conversation=parent,
            worker_conversation_id=thread.worker_conversation_id,
            message=message,
        )
        await self._jobs.enqueue(job)
        logger.info("a2a_message_enqueued", thread_id=thread_id, job_id=job.job_id)
        return A2AJobReceipt(thread_id=thread_id, job_id=job.job_id, status="queued")

    async def status(self, parent: ConversationKey, job_id: str) -> A2AJobReport:
        """Return a safe snapshot containing a completed worker answer when available."""
        job = await self._jobs.load_job(job_id, parent)
        if job is None:
            raise A2AJobNotFoundError("Worker job does not exist")
        return A2AJobReport(
            thread_id=job.thread_id,
            job_id=job.job_id,
            status=job.status,
            answer=job.answer,
            error_code=job.error_code,
        )


class A2AWorker:
    """Claim durable A2A messages and run them through the worker agent."""

    def __init__(
        self,
        jobs: A2AJobRepository,
        agent_service: AgentService,
        conversations: ConversationRepository,
        definition: AgentDefinition,
        *,
        worker_id: str,
        poll_seconds: float,
        job_timeout_seconds: float,
    ) -> None:
        """Configure one process worker with explicit polling and execution bounds."""
        self._jobs = jobs
        self._agent_service = agent_service
        self._conversations = conversations
        self._definition = definition
        self._worker_id = worker_id
        self._poll_seconds = poll_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the polling loop exactly once in the application lifespan."""
        if self._task is not None:
            raise RuntimeError("A2A worker has already been started")
        self._task = asyncio.create_task(self._run(), name=f"a2a-worker-{self._worker_id}")

    async def close(self) -> None:
        """Stop polling and requeue an interrupted active claim."""
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run_once(self) -> bool:
        """Process at most one claimed job and report whether work was available."""
        lease_seconds = self._job_timeout_seconds + 30.0
        job = await self._jobs.claim_next(self._worker_id, lease_seconds)
        if job is None:
            return False
        logger.info("a2a_job_started", thread_id=job.thread_id, job_id=job.job_id)
        try:
            await self._process_job(job)
        except asyncio.CancelledError:
            await self._requeue_safely(job.job_id)
            raise
        return True

    async def _process_job(self, job: A2AJob) -> None:
        """Recover or execute one claimed job and commit its terminal state."""
        recovered_answer = await self._recover_answer(job)
        if recovered_answer is not None:
            await self._jobs.complete(
                job.job_id,
                self._worker_id,
                recovered_answer,
                f"recovered:{job.job_id}",
                self._completion_message(job, answer=recovered_answer),
            )
            logger.info("a2a_job_recovered", thread_id=job.thread_id, job_id=job.job_id)
            return
        protocol_message = A2AMessage(message_id=job.job_id, content=job.message).serialize()
        try:
            result = await asyncio.wait_for(
                self._agent_service.run(
                    protocol_message,
                    self._definition,
                    ConversationKey(
                        conversation_id=job.worker_conversation_id,
                        user_id=job.parent_conversation.user_id,
                        tenant_id=job.parent_conversation.tenant_id,
                    ),
                    source="worker_agent",
                ),
                timeout=self._job_timeout_seconds,
            )
        except Exception as exc:
            error_code = "worker_timeout" if isinstance(exc, TimeoutError) else "worker_failed"
            logger.exception(
                "a2a_job_failed",
                thread_id=job.thread_id,
                job_id=job.job_id,
                error_type=type(exc).__name__,
            )
            await self._jobs.fail(
                job.job_id,
                self._worker_id,
                error_code,
                self._completion_message(job, error_code=error_code),
            )
        else:
            await self._jobs.complete(
                job.job_id,
                self._worker_id,
                result.answer,
                result.response_id,
                self._completion_message(job, answer=result.answer),
            )
            logger.info("a2a_job_completed", thread_id=job.thread_id, job_id=job.job_id)

    @staticmethod
    def _completion_message(
        job: A2AJob,
        *,
        answer: str | None = None,
        error_code: str | None = None,
    ) -> str:
        """Build the trusted-data envelope that wakes the interactive agent."""
        return A2ACompletionMessage(
            job_id=job.job_id,
            thread_id=job.thread_id,
            status="completed" if error_code is None else "failed",
            answer=answer,
            error_code=error_code,
        ).serialize()

    async def _requeue_safely(self, job_id: str) -> None:
        """Best-effort release of a claim while preserving cancellation semantics."""
        try:
            await asyncio.shield(self._jobs.requeue(job_id, self._worker_id))
        except Exception as exc:
            logger.warning(
                "a2a_job_requeue_failed",
                job_id=job_id,
                error_type=type(exc).__name__,
            )

    async def _recover_answer(self, job: A2AJob) -> str | None:
        """Find an already persisted A2A turn after a crash before job completion."""
        key = ConversationKey(
            conversation_id=job.worker_conversation_id,
            user_id=job.parent_conversation.user_id,
            tenant_id=job.parent_conversation.tenant_id,
        )
        conversation = await self._conversations.load(key)
        if conversation is None:
            return None
        expected = A2AMessage(message_id=job.job_id, content=job.message).serialize()
        matching_turn = False
        for item in conversation.messages:
            if isinstance(item, ConversationMessage) and item.role == "user":
                matching_turn = item.content == expected
            elif (
                matching_turn and isinstance(item, ConversationMessage) and item.role == "assistant"
            ):
                return item.content
        return None

    async def _run(self) -> None:
        """Poll durably until shutdown while keeping transient repository failures isolated."""
        while not self._stop.is_set():
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("a2a_worker_poll_failed", error_type=type(exc).__name__)
                worked = False
            if worked:
                continue
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
