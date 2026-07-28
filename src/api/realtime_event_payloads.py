from typing import Any

from api.schemas import ToolCallResponse
from domain.realtime import (
    RealtimeActivityEnded,
    RealtimeActivityStarted,
    RealtimeAgentEvent,
    RealtimeAudioInterrupted,
    RealtimeInputTranscriptDelta,
    RealtimeOutputTranscriptDelta,
    RealtimeReconnected,
    RealtimeReconnectRequested,
    RealtimeToolCompleted,
    RealtimeToolStarted,
    RealtimeTurnCompleted,
    RealtimeVisualComponent,
)
from domain.visuals import visual_presentation_payload


def realtime_event_payload(event: RealtimeAgentEvent) -> tuple[str, dict[str, Any]]:
    """Translate one non-media realtime event into a public JSON payload."""
    if isinstance(event, RealtimeInputTranscriptDelta):
        return "input_transcript_delta", {"turn_id": event.turn_id, "text": event.text}
    if isinstance(event, RealtimeOutputTranscriptDelta):
        return "output_transcript_delta", {"turn_id": event.turn_id, "text": event.text}
    if isinstance(event, RealtimeAudioInterrupted):
        return "audio_interrupted", {"turn_id": event.turn_id}
    if isinstance(event, RealtimeActivityStarted):
        return "activity_started", {"turn_id": event.turn_id}
    if isinstance(event, RealtimeActivityEnded):
        return "activity_ended", {"turn_id": event.turn_id}
    if isinstance(event, RealtimeReconnectRequested):
        return "reconnecting", {"deadline_seconds": event.deadline_seconds}
    if isinstance(event, RealtimeReconnected):
        return "reconnected", {"resumed": event.resumed}
    if isinstance(event, RealtimeToolStarted):
        return "tool_started", {
            "turn_id": event.turn_id,
            "call_id": event.call_id,
            "tool_name": event.tool_name,
        }
    if isinstance(event, RealtimeToolCompleted):
        record = event.record
        payload = ToolCallResponse(
            call_id=record.call_id,
            tool_name=record.tool_name,
            arguments=record.arguments,
            status=record.status,
            output=record.output,
            error=record.error,
            duration_ms=round(record.duration_ms, 2),
        ).model_dump(mode="json")
        return "tool_completed", {"turn_id": event.turn_id, **payload}
    if isinstance(event, RealtimeVisualComponent):
        return "visual_component", {
            "turn_id": event.turn_id,
            **visual_presentation_payload(event.presentation),
        }
    if isinstance(event, RealtimeTurnCompleted):
        return "turn_completed", {
            "turn_id": event.turn_id,
            "answer": event.result.answer,
            "response_id": event.result.response_id,
            "session_uid": event.result.conversation_id,
            "source": event.source,
            "job_id": event.job_id,
            "causation_id": event.causation_id,
            "visual_components": [
                visual_presentation_payload(component)
                for component in event.result.visual_components
            ],
        }
    raise TypeError(f"Unsupported realtime JSON event: {type(event).__name__}")
