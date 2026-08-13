from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from domain.costs import ModelCost, ModelUsage, TurnMetrics
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
    metrics: TurnMetrics | None = None

    def __post_init__(self) -> None:
        """Infer and validate provenance without changing provider-facing roles."""
        source = self.source or ("assistant" if self.role == "assistant" else "text_user")
        if self.role == "assistant" and source != "assistant":
            raise ValueError("Assistant messages must use the assistant source")
        if self.role == "user" and source == "assistant":
            raise ValueError("User messages must use an input source")
        if self.role != "assistant" and self.metrics is not None:
            raise ValueError("Only assistant messages can carry turn metrics")
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
class DailyTokenUsage:
    """Owner-global model consumption and cost aggregated into one UTC calendar day."""

    day: str
    usage: ModelUsage
    cost: ModelCost | None
    turn_count: int
    model_call_count: int
    fully_priced: bool

    def __post_init__(self) -> None:
        """Reject invalid aggregate counters at the domain boundary."""
        _validate_usage_accounting(
            cost=self.cost,
            turn_count=self.turn_count,
            model_call_count=self.model_call_count,
            fully_priced=self.fully_priced,
            scope="Daily usage",
        )


ConversationUsageRole = Literal["interactive", "worker"]


@dataclass(frozen=True, slots=True)
class ModelUsageBreakdown:
    """Aggregated usage for one model inside a larger accounting scope."""

    model: str
    usage: ModelUsage
    cost: ModelCost | None
    model_call_count: int
    fully_priced: bool

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Model breakdown name cannot be empty")
        if self.model_call_count < 1:
            raise ValueError("Model breakdown must include at least one call")
        if self.fully_priced != (self.cost is not None):
            raise ValueError("Model breakdown pricing state is inconsistent")


@dataclass(frozen=True, slots=True)
class ConversationUsageBreakdown:
    """Usage and cost for one root or worker conversation in a group."""

    conversation_id: str
    title: str
    role: ConversationUsageRole
    thread_id: str | None
    usage: ModelUsage
    cost: ModelCost | None
    turn_count: int
    model_call_count: int
    fully_priced: bool
    models: tuple[ModelUsageBreakdown, ...]

    def __post_init__(self) -> None:
        if not self.conversation_id:
            raise ValueError("Conversation usage id cannot be empty")
        if not self.title.strip():
            raise ValueError("Conversation usage title cannot be empty")
        _validate_usage_accounting(
            cost=self.cost,
            turn_count=self.turn_count,
            model_call_count=self.model_call_count,
            fully_priced=self.fully_priced,
            scope="Conversation usage",
        )
        if self.role == "interactive" and self.thread_id is not None:
            raise ValueError("Interactive usage cannot have a worker thread")
        if self.role == "worker" and self.thread_id is None:
            raise ValueError("Worker usage must include its thread")
        if sum(model.model_call_count for model in self.models) != self.model_call_count:
            raise ValueError("Conversation model breakdown call count is inconsistent")


@dataclass(frozen=True, slots=True)
class SessionUsageReport:
    """Cost and token usage for a root session plus every delegated worker."""

    root_conversation: ConversationKey
    usage: ModelUsage
    cost: ModelCost | None
    turn_count: int
    model_call_count: int
    fully_priced: bool
    models: tuple[ModelUsageBreakdown, ...]
    conversations: tuple[ConversationUsageBreakdown, ...]

    def __post_init__(self) -> None:
        _validate_usage_accounting(
            cost=self.cost,
            turn_count=self.turn_count,
            model_call_count=self.model_call_count,
            fully_priced=self.fully_priced,
            scope="Session usage",
        )
        if not self.conversations:
            raise ValueError("Session usage must include its root conversation")
        conversation_ids = [item.conversation_id for item in self.conversations]
        if len(conversation_ids) != len(set(conversation_ids)):
            raise ValueError("Session usage conversations must be unique")
        roots = [
            item
            for item in self.conversations
            if item.role == "interactive"
            and item.conversation_id == self.root_conversation.conversation_id
        ]
        if len(roots) != 1:
            raise ValueError("Session usage must identify exactly one root conversation")
        if sum(item.model_call_count for item in self.conversations) != self.model_call_count:
            raise ValueError("Session conversation call count is inconsistent")
        if sum(model.model_call_count for model in self.models) != self.model_call_count:
            raise ValueError("Session model breakdown call count is inconsistent")


def _validate_usage_accounting(
    *,
    cost: ModelCost | None,
    turn_count: int,
    model_call_count: int,
    fully_priced: bool,
    scope: str,
) -> None:
    """Keep aggregate counters and optional monetary totals semantically aligned."""
    if turn_count < 0 or model_call_count < 0:
        raise ValueError(f"{scope} counters cannot be negative")
    if turn_count > model_call_count:
        raise ValueError(f"{scope} cannot contain more turns than model calls")
    if model_call_count == 0:
        if cost is not None or not fully_priced:
            raise ValueError(f"{scope} empty pricing state is inconsistent")
        return
    if fully_priced != (cost is not None):
        raise ValueError(f"{scope} pricing state is inconsistent")


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
