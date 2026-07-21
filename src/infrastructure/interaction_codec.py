import base64
import binascii
from typing import Any, Literal, cast

from domain.agent import AgentResult
from domain.events import (
    AgentAudioDelta,
    AgentAudioInterrupted,
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentStreamFailed,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
)
from domain.tools import ToolCallRecord


def encode_agent_event(event: AgentStreamEvent) -> tuple[str, dict[str, object]]:
    """Serialize one neutral event for durable outbox storage."""
    if isinstance(event, AgentAudioDelta):
        return "audio_delta", {
            "audio": base64.b64encode(event.data).decode("ascii"),
            "mime_type": event.mime_type,
        }
    if isinstance(event, AgentAudioInterrupted):
        return "audio_interrupted", {}
    if isinstance(event, AgentTextDelta):
        return "text_delta", {"text": event.text}
    if isinstance(event, AgentToolStarted):
        return "tool_started", {"call_id": event.call_id, "tool_name": event.tool_name}
    if isinstance(event, AgentToolCompleted):
        record = event.record
        return "tool_completed", {
            "call_id": record.call_id,
            "tool_name": record.tool_name,
            "arguments": record.arguments,
            "status": record.status,
            "output": record.output,
            "error": record.error,
            "duration_ms": record.duration_ms,
        }
    if isinstance(event, AgentStreamCompleted):
        result = event.result
        return "completed", {
            "answer": result.answer,
            "response_id": result.response_id,
            "conversation_id": result.conversation_id,
            "tool_calls": [
                {
                    "call_id": record.call_id,
                    "tool_name": record.tool_name,
                    "arguments": record.arguments,
                    "status": record.status,
                    "output": record.output,
                    "error": record.error,
                    "duration_ms": record.duration_ms,
                }
                for record in result.tool_calls
            ],
        }
    if isinstance(event, AgentStreamFailed):
        return "error", {"code": event.code, "message": event.message}
    raise TypeError(f"Unsupported agent stream event: {type(event).__name__}")


def decode_agent_event(event_type: str, raw: object) -> AgentStreamEvent:
    """Validate a stored outbox payload and rebuild its neutral event."""
    if not isinstance(raw, dict):
        raise ValueError("interaction output payload must be an object")
    payload = cast(dict[str, Any], raw)
    if event_type == "audio_delta":
        encoded = _required_text(payload, "audio")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("audio must be valid base64") from exc
        return AgentAudioDelta(
            data=data,
            mime_type=_required_text(payload, "mime_type"),
        )
    if event_type == "audio_interrupted":
        return AgentAudioInterrupted()
    if event_type == "text_delta":
        return AgentTextDelta(text=_required_text(payload, "text"))
    if event_type == "tool_started":
        return AgentToolStarted(
            call_id=_required_text(payload, "call_id"),
            tool_name=_required_text(payload, "tool_name"),
        )
    if event_type == "tool_completed":
        return AgentToolCompleted(record=_decode_tool_record(payload))
    if event_type == "completed":
        raw_records = payload.get("tool_calls")
        if not isinstance(raw_records, list):
            raise ValueError("completed tool_calls must be a list")
        return AgentStreamCompleted(
            result=AgentResult(
                answer=_required_text(payload, "answer"),
                response_id=_required_text(payload, "response_id"),
                conversation_id=_required_text(payload, "conversation_id"),
                tool_calls=tuple(_decode_tool_record(record) for record in raw_records),
            )
        )
    if event_type == "error":
        return AgentStreamFailed(
            code=_required_text(payload, "code"),
            message=_required_text(payload, "message"),
        )
    raise ValueError("interaction output event type is invalid")


def _decode_tool_record(raw: object) -> ToolCallRecord:
    """Decode one tool audit record nested inside an outbox event."""
    if not isinstance(raw, dict):
        raise ValueError("tool record must be an object")
    record = cast(dict[str, Any], raw)
    arguments = record.get("arguments")
    status = record.get("status")
    error = record.get("error")
    duration_ms = record.get("duration_ms")
    if (
        not isinstance(arguments, dict)
        or status not in ("success", "error")
        or error is not None
        and not isinstance(error, str)
        or not isinstance(duration_ms, int | float)
    ):
        raise ValueError("tool record fields are invalid")
    return ToolCallRecord(
        call_id=_required_text(record, "call_id"),
        tool_name=_required_text(record, "tool_name"),
        arguments=arguments,
        status=cast(Literal["success", "error"], status),
        output=record.get("output"),
        error=error,
        duration_ms=float(duration_ms),
    )


def _required_text(payload: dict[str, Any], name: str) -> str:
    """Read one mandatory string field from a decoded payload."""
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value
