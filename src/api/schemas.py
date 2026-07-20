from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from domain.agent import AgentResult


class StreamAgentRequest(BaseModel):
    """Validated streaming input for a previously created session."""

    message: str = Field(min_length=1, max_length=20_000)
    session_uid: UUID
    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)


class CreateSessionRequest(BaseModel):
    """Ownership data required to create a persisted chat session."""

    user_id: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=128)


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
