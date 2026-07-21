import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog

from api.event_payloads import agent_event_payload
from domain.turn_events import AgentStreamEvent

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
    event_type, data = agent_event_payload(event)
    return encode_sse(event_type, data)


def encode_sse(event: str, data: dict[str, Any]) -> str:
    """Serialize one JSON payload using the Server-Sent Events wire format."""
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {encoded}\n\n"
