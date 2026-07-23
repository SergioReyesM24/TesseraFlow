from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from domain.agent import AgentResult
from domain.conversations import (
    ConversationCorrelation,
    ConversationGroup,
    ConversationHistoryPage,
    ConversationListPage,
    ConversationMessage,
)
from domain.tools import ToolCall, ToolResult


class StreamAgentRequest(BaseModel):
    """Validated input for the backwards-compatible SSE endpoint."""

    message: str = Field(min_length=1, max_length=20_000)
    session_uid: UUID
    user_id: str = Field(min_length=1, max_length=128)


class AgentWebSocketRequest(BaseModel):
    """One correlated user turn received through an established agent socket."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["message"]
    request_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1, max_length=20_000)


class RealtimeAudioStartRequest(BaseModel):
    """Begin one correlated PCM input stream on a realtime agent socket."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["audio_start"]
    turn_id: UUID = Field(default_factory=uuid4)


class RealtimeAudioEndRequest(BaseModel):
    """Pause the current PCM input stream and let provider VAD finish the turn."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["audio_end"]


class RealtimeActivityStartRequest(BaseModel):
    """Mark explicit start of user speech activity."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["activity_start"]


class RealtimeActivityEndRequest(BaseModel):
    """Mark explicit end of user speech activity."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["activity_end"]


class RealtimeTextRequest(BaseModel):
    """Send a text fallback through an established realtime model session."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    turn_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1, max_length=20_000)


class CreateSessionRequest(BaseModel):
    """Ownership data required to create a persisted chat session."""

    user_id: str = Field(min_length=1, max_length=128)


class CreateSessionResponse(BaseModel):
    """Public identifier of a newly persisted empty chat session."""

    session_uid: UUID


class ToolCallResponse(BaseModel):
    """Public representation of an executed tool call and its outcome."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    status: Literal["success", "error"]
    output: Any | None
    error: str | None
    duration_ms: float


class AgentCompletedResponse(BaseModel):
    """Terminal stream payload containing the answer and tool execution trace."""

    answer: str
    response_id: str
    session_uid: UUID
    tool_calls: list[ToolCallResponse]

    @classmethod
    def from_result(cls, result: AgentResult) -> "AgentCompletedResponse":
        """Convert the provider-neutral application result into an API schema."""
        return cls(
            answer=result.answer,
            response_id=result.response_id,
            session_uid=UUID(result.conversation_id),
            tool_calls=[
                ToolCallResponse(
                    call_id=record.call_id,
                    tool_name=record.tool_name,
                    arguments=record.arguments,
                    status=record.status,
                    output=record.output,
                    error=record.error,
                    duration_ms=round(record.duration_ms, 2),
                )
                for record in result.tool_calls
            ],
        )


class ConversationMessageHistoryPayload(BaseModel):
    """Typed canonical payload for one persisted user or assistant message."""

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    content: str
    source: Literal["text_user", "speech_user", "worker_agent", "assistant"]


class ToolCallHistoryPayload(BaseModel):
    """Typed canonical payload for arguments requested by the model."""

    type: Literal["tool_call"] = "tool_call"
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ToolResultHistoryPayload(BaseModel):
    """Typed canonical payload returned to the model for one tool call."""

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    output: Any | None
    error: str | None


ConversationHistoryPayload = Annotated[
    ConversationMessageHistoryPayload | ToolCallHistoryPayload | ToolResultHistoryPayload,
    Field(discriminator="type"),
]


class ConversationHistoryItemResponse(BaseModel):
    """One ordered PostgreSQL conversation item with its stable turn identifier."""

    sequence: int
    turn_id: UUID
    created_at: datetime
    payload: ConversationHistoryPayload


class ConversationSummaryResponse(BaseModel):
    """Technical metadata for one session in the owner's conversation browser."""

    session_uid: UUID
    title: str
    status: Literal["active", "archived"]
    version: int
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    correlation: "ConversationCorrelationResponse"


class ConversationCorrelationResponse(BaseModel):
    """Public identifiers locating one isolated history inside a root chat."""

    conversation_id: UUID
    root_conversation_id: UUID
    parent_conversation_id: UUID | None
    worker_conversation_id: UUID | None
    thread_id: UUID | None

    @classmethod
    def from_domain(
        cls,
        correlation: ConversationCorrelation,
    ) -> "ConversationCorrelationResponse":
        """Translate a validated neutral projection to public UUIDs."""
        return cls(
            conversation_id=UUID(correlation.conversation_id),
            root_conversation_id=UUID(correlation.root_conversation_id),
            parent_conversation_id=(
                UUID(correlation.parent_conversation_id)
                if correlation.parent_conversation_id is not None
                else None
            ),
            worker_conversation_id=(
                UUID(correlation.worker_conversation_id)
                if correlation.worker_conversation_id is not None
                else None
            ),
            thread_id=UUID(correlation.thread_id) if correlation.thread_id is not None else None,
        )


class ConversationJobCorrelationResponse(BaseModel):
    """A2A job identifiers shared by worker execution and parent delivery."""

    job_id: UUID
    request_id: UUID
    turn_id: UUID
    status: Literal["queued", "running", "completed", "failed", "cancelled"]


class ConversationGroupMemberResponse(BaseModel):
    """One isolated history and the A2A jobs that execute within it."""

    correlation: ConversationCorrelationResponse
    jobs: list[ConversationJobCorrelationResponse]


class ConversationGroupResponse(BaseModel):
    """Consultable projection of one root conversation and all worker histories."""

    user_id: str
    root_conversation_id: UUID
    conversations: list[ConversationGroupMemberResponse]

    @classmethod
    def from_group(cls, group: ConversationGroup) -> "ConversationGroupResponse":
        """Translate a neutral group without merging any member's message history."""
        return cls(
            user_id=group.root_conversation.user_id,
            root_conversation_id=UUID(group.root_conversation.conversation_id),
            conversations=[
                ConversationGroupMemberResponse(
                    correlation=ConversationCorrelationResponse.from_domain(member.correlation),
                    jobs=[
                        ConversationJobCorrelationResponse(
                            job_id=UUID(job.job_id),
                            request_id=UUID(job.request_id),
                            turn_id=UUID(job.turn_id),
                            status=job.status,
                        )
                        for job in member.jobs
                    ],
                )
                for member in group.members
            ],
        )


class ConversationListResponse(BaseModel):
    """Paginated owner-scoped list of persisted conversation sessions."""

    user_id: str
    sessions: list[ConversationSummaryResponse]
    has_more: bool
    next_offset: int | None

    @classmethod
    def from_page(
        cls,
        page: ConversationListPage,
        *,
        user_id: str,
        offset: int,
    ) -> "ConversationListResponse":
        """Translate neutral summaries into the public session-list contract."""
        sessions = [
            ConversationSummaryResponse(
                session_uid=UUID(session.key.conversation_id),
                title=session.title,
                status=session.status,
                version=session.version,
                last_sequence=session.last_sequence,
                created_at=session.created_at,
                updated_at=session.updated_at,
                last_message_at=session.last_message_at,
                correlation=ConversationCorrelationResponse.from_domain(session.correlation),
            )
            for session in page.sessions
        ]
        return cls(
            user_id=user_id,
            sessions=sessions,
            has_more=page.has_more,
            next_offset=offset + len(sessions) if page.has_more else None,
        )


class ConversationHistoryResponse(BaseModel):
    """Paginated technical history for an owner-scoped conversation session."""

    session_uid: UUID
    user_id: str
    title: str
    status: Literal["active", "archived"]
    version: int
    last_sequence: int
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None
    items: list[ConversationHistoryItemResponse]
    has_more: bool
    next_after_sequence: int | None
    correlation: ConversationCorrelationResponse

    @classmethod
    def from_history(cls, history: ConversationHistoryPage) -> "ConversationHistoryResponse":
        """Translate neutral canonical items into discriminated API payloads."""
        items: list[ConversationHistoryItemResponse] = []
        for record in history.items:
            item = record.item
            payload: ConversationHistoryPayload
            if isinstance(item, ConversationMessage):
                payload = ConversationMessageHistoryPayload(
                    role=item.role,
                    content=item.content,
                    source=item.source,
                )
            elif isinstance(item, ToolCall):
                payload = ToolCallHistoryPayload(
                    call_id=item.call_id,
                    tool_name=item.tool_name,
                    arguments=item.arguments,
                )
            elif isinstance(item, ToolResult):
                payload = ToolResultHistoryPayload(
                    call_id=item.call_id,
                    output=item.output,
                    error=item.error,
                )
            else:
                raise TypeError(f"Unsupported conversation item: {type(item).__name__}")
            items.append(
                ConversationHistoryItemResponse(
                    sequence=record.sequence,
                    turn_id=UUID(record.turn_id),
                    created_at=record.created_at,
                    payload=payload,
                )
            )
        next_sequence = items[-1].sequence if history.has_more and items else None
        return cls(
            session_uid=UUID(history.key.conversation_id),
            user_id=history.key.user_id,
            title=history.title,
            status=history.status,
            version=history.version,
            last_sequence=history.last_sequence,
            created_at=history.created_at,
            updated_at=history.updated_at,
            last_message_at=history.last_message_at,
            items=items,
            has_more=history.has_more,
            next_after_sequence=next_sequence,
            correlation=ConversationCorrelationResponse.from_domain(history.correlation),
        )
