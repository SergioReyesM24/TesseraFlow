from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from domain.agent import AgentResult
from domain.conversations import (
    ConversationCorrelation,
    ConversationGroup,
    ConversationHistoryPage,
    ConversationListPage,
    ConversationMessage,
    ConversationUsageBreakdown,
    DailyTokenUsage,
    ModelUsageBreakdown,
    SessionUsageReport,
)
from domain.costs import ModelUsage, TurnMetrics
from domain.evaluations import EvaluationTrace
from domain.tools import ToolCall, ToolResult
from domain.visuals import visual_presentation_payload


def _format_datetime(value: datetime | None) -> str | None:
    """Serialize API timestamps with the product-wide DD-MM-YYYY date prefix."""
    if value is None:
        return None
    fraction = f".{value.microsecond:06d}".rstrip("0") if value.microsecond else ""
    offset = value.strftime("%z")
    if offset == "+0000":
        timezone = "Z"
    elif offset:
        timezone = f"{offset[:3]}:{offset[3:]}"
    else:
        timezone = ""
    return f"{value:%d-%m-%YT%H:%M:%S}{fraction}{timezone}"


class DateFormattedResponse(BaseModel):
    """Apply the public date format to timestamp fields on response models."""

    @field_serializer("created_at", "updated_at", "last_message_at", check_fields=False)
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return _format_datetime(value)


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


class ModelUsageResponse(BaseModel):
    """Provider-neutral token counters suitable for API consumers."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    uncached_input_tokens: int
    cached_input_audio_tokens: int
    reasoning_tokens: int
    input_audio_tokens: int
    output_audio_tokens: int

    @classmethod
    def from_domain(cls, usage: ModelUsage) -> "ModelUsageResponse":
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            uncached_input_tokens=usage.uncached_input_tokens,
            cached_input_audio_tokens=usage.cached_input_audio_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            input_audio_tokens=usage.input_audio_tokens,
            output_audio_tokens=usage.output_audio_tokens,
        )


class ModelCostResponse(BaseModel):
    """Calculated cost in the configured rate-card currency."""

    amount: float
    currency: str


class DailyTokenUsageResponse(BaseModel):
    """One UTC day of owner-global model consumption."""

    date: str
    usage: ModelUsageResponse
    cost: ModelCostResponse | None
    turn_count: int
    model_call_count: int
    fully_priced: bool

    @classmethod
    def from_domain(cls, value: DailyTokenUsage) -> "DailyTokenUsageResponse":
        return cls(
            date=value.day,
            usage=ModelUsageResponse.from_domain(value.usage),
            cost=(
                ModelCostResponse(amount=float(value.cost.amount), currency=value.cost.currency)
                if value.cost is not None
                else None
            ),
            turn_count=value.turn_count,
            model_call_count=value.model_call_count,
            fully_priced=value.fully_priced,
        )


class DailyTokenUsageReportResponse(BaseModel):
    """Daily usage window across all conversations owned by a user."""

    user_id: str
    timezone: Literal["UTC"] = "UTC"
    days: list[DailyTokenUsageResponse]


class ModelUsageBreakdownResponse(BaseModel):
    """Aggregated token and cost totals for one model."""

    model: str
    usage: ModelUsageResponse
    cost: ModelCostResponse | None
    model_call_count: int
    fully_priced: bool

    @classmethod
    def from_domain(cls, value: ModelUsageBreakdown) -> "ModelUsageBreakdownResponse":
        return cls(
            model=value.model,
            usage=ModelUsageResponse.from_domain(value.usage),
            cost=(
                ModelCostResponse(amount=float(value.cost.amount), currency=value.cost.currency)
                if value.cost is not None
                else None
            ),
            model_call_count=value.model_call_count,
            fully_priced=value.fully_priced,
        )


class ConversationUsageBreakdownResponse(BaseModel):
    """Aggregated token and cost totals for one root or worker conversation."""

    conversation_id: UUID
    title: str
    role: Literal["interactive", "worker"]
    thread_id: UUID | None
    usage: ModelUsageResponse
    cost: ModelCostResponse | None
    turn_count: int
    model_call_count: int
    fully_priced: bool
    models: list[ModelUsageBreakdownResponse]

    @classmethod
    def from_domain(
        cls,
        value: ConversationUsageBreakdown,
    ) -> "ConversationUsageBreakdownResponse":
        return cls(
            conversation_id=UUID(value.conversation_id),
            title=value.title,
            role=value.role,
            thread_id=UUID(value.thread_id) if value.thread_id is not None else None,
            usage=ModelUsageResponse.from_domain(value.usage),
            cost=(
                ModelCostResponse(amount=float(value.cost.amount), currency=value.cost.currency)
                if value.cost is not None
                else None
            ),
            turn_count=value.turn_count,
            model_call_count=value.model_call_count,
            fully_priced=value.fully_priced,
            models=[ModelUsageBreakdownResponse.from_domain(model) for model in value.models],
        )


class SessionUsageReportResponse(BaseModel):
    """Full cost report for a root session plus delegated worker conversations."""

    user_id: str
    root_conversation_id: UUID
    usage: ModelUsageResponse
    cost: ModelCostResponse | None
    turn_count: int
    model_call_count: int
    fully_priced: bool
    models: list[ModelUsageBreakdownResponse]
    conversations: list[ConversationUsageBreakdownResponse]

    @classmethod
    def from_domain(cls, value: SessionUsageReport) -> "SessionUsageReportResponse":
        return cls(
            user_id=value.root_conversation.user_id,
            root_conversation_id=UUID(value.root_conversation.conversation_id),
            usage=ModelUsageResponse.from_domain(value.usage),
            cost=(
                ModelCostResponse(amount=float(value.cost.amount), currency=value.cost.currency)
                if value.cost is not None
                else None
            ),
            turn_count=value.turn_count,
            model_call_count=value.model_call_count,
            fully_priced=value.fully_priced,
            models=[ModelUsageBreakdownResponse.from_domain(model) for model in value.models],
            conversations=[
                ConversationUsageBreakdownResponse.from_domain(conversation)
                for conversation in value.conversations
            ],
        )


class ModelCallMetricsResponse(BaseModel):
    """Accounting detail for one model request inside a logical turn."""

    model: str
    usage: ModelUsageResponse
    cost: ModelCostResponse | None


class TurnMetricsResponse(BaseModel):
    """Aggregated accounting plus the auditable request-level breakdown."""

    usage: ModelUsageResponse
    cost: ModelCostResponse | None
    calls: list[ModelCallMetricsResponse]

    @classmethod
    def from_domain(cls, metrics: TurnMetrics) -> "TurnMetricsResponse":
        total_cost = metrics.cost
        return cls(
            usage=ModelUsageResponse.from_domain(metrics.usage),
            cost=(
                ModelCostResponse(amount=float(total_cost.amount), currency=total_cost.currency)
                if total_cost is not None
                else None
            ),
            calls=[
                ModelCallMetricsResponse(
                    model=call.model,
                    usage=ModelUsageResponse.from_domain(call.usage),
                    cost=(
                        ModelCostResponse(
                            amount=float(call.cost.amount),
                            currency=call.cost.currency,
                        )
                        if call.cost is not None
                        else None
                    ),
                )
                for call in metrics.calls
            ],
        )


class AgentCompletedResponse(BaseModel):
    """Terminal stream payload containing the answer and tool execution trace."""

    answer: str
    response_id: str
    session_uid: UUID
    tool_calls: list[ToolCallResponse]
    visual_components: list[dict[str, Any]]
    metrics: TurnMetricsResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

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
            visual_components=[
                visual_presentation_payload(component) for component in result.visual_components
            ],
            metrics=(
                TurnMetricsResponse.from_domain(result.metrics) if result.metrics.calls else None
            ),
        )


class ConversationMessageHistoryPayload(BaseModel):
    """Typed canonical payload for one persisted user or assistant message."""

    type: Literal["message"] = "message"
    role: Literal["user", "assistant"]
    content: str
    source: Literal["text_user", "speech_user", "worker_agent", "assistant"]
    metrics: TurnMetricsResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


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


class ConversationHistoryItemResponse(DateFormattedResponse):
    """One ordered PostgreSQL conversation item with its stable turn identifier."""

    sequence: int
    turn_id: UUID
    created_at: datetime
    payload: ConversationHistoryPayload


class ConversationSummaryResponse(DateFormattedResponse):
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


class EvaluationTraceResponse(DateFormattedResponse):
    """Safe evaluator audit record exposed without provider prompts or raw feedback."""

    trace_id: UUID
    conversation_id: UUID
    turn_id: UUID
    job_id: UUID | None
    attempt: int
    proposed_call_ids: list[str]
    verdict: Literal["pass", "fail", "uncertain"]
    risk: Literal["low", "medium", "high"]
    reason_code: Literal[
        "none",
        "wrong_tool",
        "unnecessary_tool",
        "ungrounded_arguments",
        "duplicate_action",
        "unsafe_side_effect",
        "incomplete_request",
        "other",
    ]
    feedback: str
    mode: Literal["shadow", "enforce"]
    executed: bool
    model: str
    usage: ModelUsageResponse
    latency_ms: float
    created_at: datetime

    @classmethod
    def from_domain(cls, trace: EvaluationTrace) -> "EvaluationTraceResponse":
        return cls(
            trace_id=UUID(trace.trace_id),
            conversation_id=UUID(trace.conversation_id),
            turn_id=UUID(trace.turn_id),
            job_id=UUID(trace.job_id) if trace.job_id is not None else None,
            attempt=trace.attempt,
            proposed_call_ids=list(trace.proposed_call_ids),
            verdict=trace.verdict,
            risk=trace.risk,
            reason_code=trace.reason_code,
            feedback=trace.feedback,
            mode=trace.mode,
            executed=trace.executed,
            model=trace.model,
            usage=ModelUsageResponse.from_domain(trace.usage),
            latency_ms=round(trace.latency_ms, 2),
            created_at=trace.created_at,
        )


class ConversationHistoryResponse(DateFormattedResponse):
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
    evaluations: list[EvaluationTraceResponse]
    evaluations_truncated: bool
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
                    metrics=(
                        TurnMetricsResponse.from_domain(item.metrics)
                        if item.metrics is not None and item.metrics.calls
                        else None
                    ),
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
            evaluations=[
                EvaluationTraceResponse.from_domain(trace)
                for trace in history.evaluations.traces
            ],
            evaluations_truncated=history.evaluations.truncated,
            has_more=history.has_more,
            next_after_sequence=next_sequence,
            correlation=ConversationCorrelationResponse.from_domain(history.correlation),
        )
