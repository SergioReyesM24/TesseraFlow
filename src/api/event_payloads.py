from typing import Any

from api.schemas import AgentCompletedResponse, ToolCallResponse
from domain.events import (
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
)


def agent_event_payload(event: AgentStreamEvent) -> tuple[str, dict[str, Any]]:
    """Translate one neutral agent event into a transport-independent payload."""
    if isinstance(event, AgentTextDelta):
        return "text_delta", {"text": event.text}
    if isinstance(event, AgentToolStarted):
        return "tool_started", {"call_id": event.call_id, "tool_name": event.tool_name}
    if isinstance(event, AgentToolCompleted):
        record = event.record
        tool_payload = ToolCallResponse(
            call_id=record.call_id,
            tool_name=record.tool_name,
            arguments=record.arguments,
            status=record.status,
            output=record.output,
            error=record.error,
            duration_ms=round(record.duration_ms, 2),
        )
        return "tool_completed", tool_payload.model_dump(mode="json")
    if isinstance(event, AgentStreamCompleted):
        completed_payload = AgentCompletedResponse.from_result(event.result)
        return "completed", completed_payload.model_dump(mode="json")
    raise TypeError(f"Unsupported agent stream event: {type(event).__name__}")
