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
class ConversationCorrelation:
    """Identity projection that relates one isolated session to its root chat."""

    conversation_id: str
    root_conversation_id: str
    parent_conversation_id: str | None = None
    worker_conversation_id: str | None = None
    thread_id: str | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous projections that could merge independent histories."""
        is_primary = self.parent_conversation_id is None
        if is_primary:
            if (
                self.conversation_id != self.root_conversation_id
                or self.worker_conversation_id is not None
                or self.thread_id is not None
            ):
                raise ValueError("Primary conversation correlation is inconsistent")
            return
        if (
            self.parent_conversation_id != self.root_conversation_id
            or self.worker_conversation_id != self.conversation_id
            or self.thread_id is None
            or self.conversation_id == self.root_conversation_id
        ):
            raise ValueError("Worker conversation correlation is inconsistent")


@dataclass(frozen=True, slots=True)
class ConversationJobCorrelation:
    """Identifiers that join one A2A job to both correlated conversation turns."""

    job_id: str
    request_id: str
    turn_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ConversationGroupMember:
    """One independently persisted conversation within a root conversation group."""

    correlation: ConversationCorrelation
    jobs: tuple[ConversationJobCorrelation, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationGroup:
    """Owner-scoped projection of a primary conversation and all worker sessions."""

    root_conversation: ConversationKey
    members: tuple[ConversationGroupMember, ...]


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
    correlation: ConversationCorrelation


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
    correlation: ConversationCorrelation


@dataclass(frozen=True, slots=True)
class Conversation:
    """Versioned conversation aggregate containing provider-neutral history items."""

    key: ConversationKey
    messages: tuple[ConversationItem, ...] = ()
    version: int = 0
    title: str | None = None
