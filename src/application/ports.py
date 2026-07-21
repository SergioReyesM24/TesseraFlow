from collections.abc import AsyncIterator
from typing import Protocol

from domain.a2a import A2AJob, A2AThread
from domain.agent import AgentDefinition
from domain.conversations import Conversation, ConversationItem, ConversationKey
from domain.events import ModelStreamEvent
from domain.model import ModelReply
from domain.tools import ToolResult, ToolSpec


class ModelSession(Protocol):
    """Request-scoped conversation with a model provider."""

    async def send_message(self, message: str) -> ModelReply:
        """Start the session with one user message and return a normalized reply."""
        ...

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        """Continue the session after resolving every pending tool call."""
        ...

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Stream the initial model turn as provider-neutral events."""
        ...

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Stream the model turn that follows a complete tool-result batch."""
        ...


class ModelGateway(Protocol):
    """Creates isolated sessions while sharing provider-level resources."""

    def create_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> ModelSession:
        """Create an isolated session backed by shared provider resources."""
        ...


class ConversationRepository(Protocol):
    """Persists neutral conversations with ownership and version checks."""

    async def create(self, key: ConversationKey) -> Conversation:
        """Create an empty owned conversation before it can receive messages."""
        ...

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Load a conversation only when the supplied owner matches."""
        ...

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Atomically append one complete turn at the expected conversation version."""
        ...

    async def delete(self, key: ConversationKey) -> bool:
        """Delete an owned conversation and report whether it existed."""
        ...


class ConversationCache(Protocol):
    """Caches bounded conversation context without becoming its source of truth."""

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Return cached context when present and owned by the supplied principal."""
        ...

    async def store(self, conversation: Conversation) -> None:
        """Store one already-compacted conversation context with a finite lifetime."""
        ...

    async def invalidate(self, key: ConversationKey) -> None:
        """Remove cached context for one conversation."""
        ...


class A2AJobRepository(Protocol):
    """Persist and claim ordered messages exchanged between two agents."""

    async def create_thread(self, thread: A2AThread, first_job: A2AJob) -> None:
        """Atomically create a worker thread and enqueue its initial message."""
        ...

    async def load_thread(
        self,
        thread_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AThread | None:
        """Load a thread only when it belongs to the supplied parent conversation."""
        ...

    async def enqueue(self, job: A2AJob) -> None:
        """Append one message to an existing A2A thread."""
        ...

    async def load_job(
        self,
        job_id: str,
        parent_conversation: ConversationKey,
    ) -> A2AJob | None:
        """Load a job only through its owning user conversation."""
        ...

    async def claim_next(self, worker_id: str, lease_seconds: float) -> A2AJob | None:
        """Claim the oldest runnable message while serializing each A2A thread."""
        ...

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        answer: str,
        response_id: str,
    ) -> None:
        """Store a successful worker response for the active claim."""
        ...

    async def fail(self, job_id: str, worker_id: str, error_code: str) -> None:
        """Store a safe failure code for the active claim."""
        ...

    async def requeue(self, job_id: str, worker_id: str) -> None:
        """Release an interrupted claim so another worker can resume it."""
        ...
