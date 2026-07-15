from collections.abc import AsyncIterator
from typing import Protocol

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
