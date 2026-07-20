import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog

from api.schemas import AgentCompletedResponse, ToolCallResponse
from domain.events import (
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
)

logger = structlog.get_logger(__name__)


async def encode_agent_stream(
    events: AsyncIterator[AgentStreamEvent],
) -> AsyncIterator[str]:
    """Encode neutral agent events as SSE and close cleanly on disconnection."""
    try:
        async for event in events:
            yield encode_agent_event(event)
    except asyncio.CancelledError:
        logger.info("agent_sse_disconnected")
        raise
    except Exception as exc:
        logger.exception("agent_sse_failed", error_type=type(exc).__name__)
        yield encode_sse(
            "error",
            {
                "error_type": type(exc).__name__,
                "message": "The agent stream could not be completed.",
            },
        )


def encode_agent_event(event: AgentStreamEvent) -> str:
    """Translate one typed application event into its public SSE representation."""
    if isinstance(event, AgentTextDelta):
        return encode_sse("text_delta", {"text": event.text})
    if isinstance(event, AgentToolStarted):
        return encode_sse(
            "tool_started",
            {"call_id": event.call_id, "tool_name": event.tool_name},
        )
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
        return encode_sse("tool_completed", tool_payload.model_dump(mode="json"))
    if isinstance(event, AgentStreamCompleted):
        completed_payload = AgentCompletedResponse.from_result(event.result)
        return encode_sse("completed", completed_payload.model_dump(mode="json"))
    raise TypeError(f"Unsupported agent stream event: {type(event).__name__}")


def encode_sse(event: str, data: dict[str, Any]) -> str:
    """Serialize one JSON payload using the Server-Sent Events wire format."""
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {encoded}\n\n"
