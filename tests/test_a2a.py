import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from application.a2a import A2AJobNotFoundError, A2AService, A2AWorker
from application.agent import AgentService
from application.conversations import ConversationConflictError
from application.tools import ToolRegistry
from domain.a2a import A2ACompletionMessage, A2AJob, A2AMessage, A2AThread
from domain.agent import AgentDefinition
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.model import ModelReply
from domain.tools import ToolCall, ToolResult, ToolSpec
from domain.turn_events import ModelStreamCompleted, ModelStreamEvent
from tools.registry import build_interactive_tool_registry


class InMemoryConversationRepository:
    """Store isolated conversations for A2A application tests."""

    def __init__(self) -> None:
        """Initialize an empty versioned conversation map."""
        self.conversations: dict[str, Conversation] = {}

    async def create(self, key: ConversationKey) -> Conversation:
        """Create one empty conversation."""
        conversation = Conversation(key=key)
        self.conversations[key.conversation_id] = conversation
        return conversation

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Load a conversation only for its exact owner."""
        conversation = self.conversations.get(key.conversation_id)
        if conversation is None or conversation.key != key:
            return None
        return conversation

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Append a complete turn using optimistic version checks."""
        current = self.conversations[conversation.key.conversation_id]
        if current.version != conversation.version:
            raise ConversationConflictError
        saved = replace(
            conversation,
            messages=conversation.messages + turn,
            version=conversation.version + 1,
        )
        self.conversations[conversation.key.conversation_id] = saved
        return saved

    async def delete(self, key: ConversationKey) -> bool:
        """Delete one exactly owned conversation."""
        conversation = self.conversations.get(key.conversation_id)
        if conversation is None or conversation.key != key:
            return False
        del self.conversations[key.conversation_id]
        return True


class InMemoryA2ASubscription:
    """Track monotonic job notifications without relying on wall-clock polling."""

    def __init__(self) -> None:
        """Initialize one local notification generation."""
        self.generation = 0
        self.event = asyncio.Event()

    def checkpoint(self) -> int:
        """Return the generation observed before querying durable state."""
        return self.generation

    async def wait_for_change(self, checkpoint: int, deadline_seconds: float) -> None:
        """Wait for a newer job hint or the reconciliation deadline."""
        if self.generation != checkpoint:
            return
        self.event.clear()
        if self.generation != checkpoint:
            return
        await asyncio.wait_for(self.event.wait(), timeout=deadline_seconds)

    def publish(self) -> None:
        """Advance the generation and wake the worker."""
        self.generation += 1
        self.event.set()


class InMemoryA2AJobNotifier:
    """Fan durable job hints out to process-local test subscriptions."""

    def __init__(self) -> None:
        """Initialize subscribers and an observable subscription barrier."""
        self.subscriptions: set[InMemoryA2ASubscription] = set()
        self.subscribed = asyncio.Event()

    @asynccontextmanager
    async def subscribe_jobs(self) -> AsyncIterator[InMemoryA2ASubscription]:
        """Register one worker subscription for its task lifetime."""
        subscription = InMemoryA2ASubscription()
        self.subscriptions.add(subscription)
        self.subscribed.set()
        try:
            yield subscription
        finally:
            self.subscriptions.discard(subscription)

    def publish_job(self) -> None:
        """Wake every local worker so the repository can choose one claimant."""
        for subscription in tuple(self.subscriptions):
            subscription.publish()


class InMemoryA2AJobRepository:
    """Implement ordered A2A claims without an external database."""

    def __init__(self, notifier: InMemoryA2AJobNotifier | None = None) -> None:
        """Initialize thread, job, and claim state."""
        self.notifier = notifier
        self.threads: dict[str, A2AThread] = {}
        self.jobs: dict[str, A2AJob] = {}
        self.order: list[str] = []
        self.claims: dict[str, str] = {}
        self.notifications: list[str] = []
        self.completed = asyncio.Event()

    async def create_thread(self, thread: A2AThread, first_job: A2AJob) -> None:
        """Store a thread and its initial job."""
        self.threads[thread.thread_id] = thread
        await self.enqueue(first_job)

    async def load_thread(
        self,
        thread_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AThread | None:
        """Return a thread only for its exact parent conversation."""
        thread = self.threads.get(thread_id)
        if thread is None or thread.parent_conversation != parent_conversation:
            return None
        return thread

    async def enqueue(self, job: A2AJob) -> None:
        """Append one queued job in deterministic order."""
        self.jobs[job.job_id] = job
        self.order.append(job.job_id)
        if self.notifier is not None:
            self.notifier.publish_job()

    async def load_job(
        self,
        job_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AJob | None:
        """Return a job only for its exact parent conversation."""
        job = self.jobs.get(job_id)
        if job is None or job.parent_conversation != parent_conversation:
            return None
        return job

    async def claim_next(self, worker_id: str, lease_seconds: float) -> A2AJob | None:
        """Claim the oldest job whose thread has no earlier unfinished message."""
        del lease_seconds
        for position, job_id in enumerate(self.order):
            job = self.jobs[job_id]
            if job.status != "queued":
                continue
            earlier = (self.jobs[value] for value in self.order[:position])
            if any(
                value.thread_id == job.thread_id and value.status in {"queued", "running"}
                for value in earlier
            ):
                continue
            claimed = replace(job, status="running")
            self.jobs[job_id] = claimed
            self.claims[job_id] = worker_id
            return claimed
        return None

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        answer: str,
        response_id: str,
        notification_message: str,
    ) -> None:
        """Store a completed answer for the active owner."""
        assert self.claims.pop(job_id) == worker_id
        self.jobs[job_id] = replace(
            self.jobs[job_id],
            status="completed",
            answer=answer,
            response_id=response_id,
        )
        self.notifications.append(notification_message)
        self.completed.set()

    async def fail(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        notification_message: str,
    ) -> None:
        """Store a safe failure for the active owner."""
        assert self.claims.pop(job_id) == worker_id
        self.jobs[job_id] = replace(
            self.jobs[job_id],
            status="failed",
            error_code=error_code,
        )
        self.notifications.append(notification_message)
        self.completed.set()

    async def requeue(self, job_id: str, worker_id: str) -> None:
        """Release a claim back to queued state."""
        assert self.claims.pop(job_id) == worker_id
        self.jobs[job_id] = replace(self.jobs[job_id], status="queued")
        if self.notifier is not None:
            self.notifier.publish_job()


class StubModelSession:
    """Return one preconfigured final model reply per worker turn."""

    def __init__(self, replies: list[ModelReply]) -> None:
        """Queue all replies produced within one isolated model session."""
        self.replies = deque(replies)

    async def send_message(self, message: str) -> ModelReply:
        """Return the reply for a non-streaming worker request."""
        del message
        return self.replies.popleft()

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        """Return the next reply after a complete tool-result batch."""
        del results
        return self.replies.popleft()

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Return an unused stream implementation required by the port."""
        del message
        return self._stream()

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Return an unused continuation stream required by the port."""
        del results
        return self._stream()

    async def _stream(self) -> AsyncIterator[ModelStreamEvent]:
        """Emit the configured reply as one terminal event."""
        yield ModelStreamCompleted(reply=self.replies.popleft())


class StubModelGateway:
    """Capture the durable worker history supplied to each new model session."""

    def __init__(self, session_replies: list[list[ModelReply]]) -> None:
        """Queue replies by session and initialize history observations."""
        self.session_replies = deque(session_replies)
        self.histories: list[tuple[ConversationItem, ...]] = []

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncIterator[StubModelSession]:
        """Record the worker context before creating its isolated session."""
        del definition, tools
        self.histories.append(history)
        yield StubModelSession(self.session_replies.popleft())


def deterministic_uids() -> Callable[[], UUID]:
    """Return a callable producing stable UUIDs for thread, job, and conversation IDs."""
    values = iter(UUID(int=value) for value in range(1, 20))
    return lambda: next(values)


def parent_key(user_id: str = "user-1") -> ConversationKey:
    """Build the interactive conversation ownership context."""
    return ConversationKey(conversation_id="parent-1", user_id=user_id)


async def test_worker_keeps_its_own_history_across_a2a_followups() -> None:
    """Treat primary-agent follow-ups as human messages in one worker conversation."""
    conversations = InMemoryConversationRepository()
    jobs = InMemoryA2AJobRepository()
    service = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    gateway = StubModelGateway(
        [
            [ModelReply(response_id="worker-1", text="Informe inicial con contexto adicional")],
            [ModelReply(response_id="worker-2", text="Ampliación basada en el informe")],
        ]
    )
    agent = AgentService(gateway, ToolRegistry([]), conversations)
    worker = A2AWorker(
        jobs,
        InMemoryA2AJobNotifier(),
        agent,
        conversations,
        AgentDefinition(model="worker-model", instructions="Worker", tool_names=()),
        worker_id="process-1",
        reconciliation_seconds=0.01,
        job_timeout_seconds=5,
    )

    first = await service.delegate(parent_key(), "Investiga la petición con tus tools")
    assert await worker.run_once() is True
    first_report = await service.status(parent_key(), first.job_id)
    followup = await service.continue_thread(
        parent_key(), first.thread_id, "Amplía el segundo punto"
    )
    assert await worker.run_once() is True
    followup_report = await service.status(parent_key(), followup.job_id)

    assert first_report.status == "completed"
    assert first_report.answer == "Informe inicial con contexto adicional"
    assert followup_report.answer == "Ampliación basada en el informe"
    assert gateway.histories[0] == ()
    assert gateway.histories[1] == (
        ConversationMessage(
            role="user",
            content=A2AMessage(
                message_id=first.job_id,
                content="Investiga la petición con tus tools",
            ).serialize(),
            source="worker_agent",
        ),
        ConversationMessage(
            role="assistant",
            content="Informe inicial con contexto adicional",
            source="assistant",
        ),
    )
    assert (
        jobs.notifications[0]
        == A2ACompletionMessage(
            job_id=first.job_id,
            thread_id=first.thread_id,
            status="completed",
            answer="Informe inicial con contexto adicional",
        ).serialize()
    )


async def test_interactive_agent_delegates_without_receiving_worker_tools() -> None:
    """Expose only A2A protocol tools and persist their receipt in the user chat."""
    conversations = InMemoryConversationRepository()
    await conversations.create(parent_key())
    jobs = InMemoryA2AJobRepository()
    protocol = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    interactive_tools = build_interactive_tool_registry(protocol)
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="primary-1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call-delegate",
                            tool_name="delegate_to_worker_agent",
                            arguments={"message": "Calcula y aporta detalles útiles"},
                        ),
                    ),
                ),
                ModelReply(response_id="primary-2", text="He delegado el trabajo."),
            ]
        ]
    )
    agent = AgentService(gateway, interactive_tools, conversations)

    result = await agent.run(
        "Necesito un cálculo pesado",
        AgentDefinition(
            model="interactive-model",
            instructions="Interactive",
            tool_names=interactive_tools.names,
        ),
        parent_key(),
    )

    assert interactive_tools.names == (
        "delegate_to_worker_agent",
        "get_worker_agent_status",
        "continue_worker_agent",
    )
    assert result.answer == "He delegado el trabajo."
    assert result.tool_calls[0].output is not None
    assert result.tool_calls[0].output["status"] == "queued"
    assert len(jobs.jobs) == 1


async def test_a2a_status_is_scoped_to_the_parent_conversation() -> None:
    """Prevent another user or chat from discovering a delegated job."""
    conversations = InMemoryConversationRepository()
    jobs = InMemoryA2AJobRepository()
    service = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    receipt = await service.delegate(parent_key(), "Trabajo privado")

    with pytest.raises(A2AJobNotFoundError):
        await service.status(parent_key("other-user"), receipt.job_id)


async def test_a2a_queue_serializes_messages_within_one_thread() -> None:
    """Keep a follow-up blocked while an earlier message in its thread is running."""
    conversations = InMemoryConversationRepository()
    jobs = InMemoryA2AJobRepository()
    service = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    first = await service.delegate(parent_key(), "Primero")
    second = await service.continue_thread(parent_key(), first.thread_id, "Después")

    claimed = await jobs.claim_next("worker-1", 30)
    blocked = await jobs.claim_next("worker-2", 30)
    assert claimed is not None
    assert claimed.job_id == first.job_id
    assert blocked is None

    await jobs.complete(first.job_id, "worker-1", "Hecho", "resp-1", "notification")
    next_job = await jobs.claim_next("worker-2", 30)
    assert next_job is not None
    assert next_job.job_id == second.job_id


async def test_worker_recovers_a_persisted_turn_without_calling_the_model_twice() -> None:
    """Complete a reclaimed job from history after a crash in the commit window."""
    conversations = InMemoryConversationRepository()
    jobs = InMemoryA2AJobRepository()
    service = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    receipt = await service.delegate(parent_key(), "Mensaje con identidad estable")
    claimed = await jobs.claim_next("dead-process", 30)
    assert claimed is not None
    gateway = StubModelGateway([[ModelReply(response_id="worker-1", text="Respuesta durable")]])
    agent = AgentService(gateway, ToolRegistry([]), conversations)
    worker_key = ConversationKey(
        conversation_id=claimed.worker_conversation_id,
        user_id=claimed.parent_conversation.user_id,
    )
    await agent.run(
        A2AMessage(message_id=claimed.job_id, content=claimed.message).serialize(),
        AgentDefinition(model="worker-model", instructions="Worker", tool_names=()),
        worker_key,
    )
    await jobs.requeue(claimed.job_id, "dead-process")
    recovering_worker = A2AWorker(
        jobs,
        InMemoryA2AJobNotifier(),
        agent,
        conversations,
        AgentDefinition(model="worker-model", instructions="Worker", tool_names=()),
        worker_id="new-process",
        reconciliation_seconds=0.01,
        job_timeout_seconds=5,
    )

    assert await recovering_worker.run_once() is True
    report = await service.status(parent_key(), receipt.job_id)

    assert report.status == "completed"
    assert report.answer == "Respuesta durable"
    assert len(gateway.histories) == 1


async def test_job_notification_wakes_background_worker_without_reconciliation_delay() -> None:
    """Start a newly queued A2A job from its hint instead of the slow fallback."""
    notifier = InMemoryA2AJobNotifier()
    conversations = InMemoryConversationRepository()
    jobs = InMemoryA2AJobRepository(notifier)
    service = A2AService(jobs, conversations, uid_factory=deterministic_uids())
    gateway = StubModelGateway([[ModelReply(response_id="worker-1", text="Respuesta inmediata")]])
    agent = AgentService(gateway, ToolRegistry([]), conversations)
    worker = A2AWorker(
        jobs,
        notifier,
        agent,
        conversations,
        AgentDefinition(model="worker-model", instructions="Worker", tool_names=()),
        worker_id="process-1",
        reconciliation_seconds=30,
        job_timeout_seconds=5,
    )
    worker.start()
    await notifier.subscribed.wait()

    try:
        receipt = await service.delegate(parent_key(), "Trabajo notificado")
        async with asyncio.timeout(0.5):
            await jobs.completed.wait()
    finally:
        await worker.close()

    report = await service.status(parent_key(), receipt.job_id)
    assert report.status == "completed"
    assert report.answer == "Respuesta inmediata"
