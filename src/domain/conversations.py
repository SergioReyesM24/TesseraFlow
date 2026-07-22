from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from domain.tools import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """Stable ownership context used to address one conversation safely."""

    conversation_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """Provider-neutral message retained between agent executions."""

    role: Literal["user", "assistant"]
    content: str
    source: Literal["text_user", "speech_user", "worker_agent", "assistant"] | None = None

    def __post_init__(self) -> None:
        """Infer and validate provenance without changing provider-facing roles."""
        source = self.source or ("assistant" if self.role == "assistant" else "text_user")
        if self.role == "assistant" and source != "assistant":
            raise ValueError("Assistant messages must use the assistant source")
        if self.role == "user" and source == "assistant":
            raise ValueError("User messages must use an input source")
        object.__setattr__(self, "source", source)


ConversationItem: TypeAlias = ConversationMessage | ToolCall | ToolResult


@dataclass(frozen=True, slots=True)
class ConversationHistoryItem:
    """One canonical persisted item with its database ordering metadata."""

    sequence: int
    turn_id: str
    created_at: datetime
    item: ConversationItem


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Technical metadata used to browse one user's persisted conversations."""

    key: ConversationKey
    title: str
    status: Literal["active", "archived"]
    version: int
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


@dataclass(frozen=True, slots=True)
class ConversationListPage:
    """Bounded page of persisted conversation summaries."""

    sessions: tuple[ConversationSummary, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class ConversationHistoryPage:
    """Bounded technical view of one owned conversation and its canonical items."""

    key: ConversationKey
    title: str
    status: Literal["active", "archived"]
    version: int
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    items: tuple[ConversationHistoryItem, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class Conversation:
    """Versioned conversation aggregate containing provider-neutral history items."""

    key: ConversationKey
    messages: tuple[ConversationItem, ...] = ()
    version: int = 0
    title: str | None = None
