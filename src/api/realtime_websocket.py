import asyncio
from typing import Annotated

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import Field, TypeAdapter, ValidationError

from api.realtime_event_payloads import realtime_event_payload
from api.schemas import (
    RealtimeAudioEndRequest,
    RealtimeAudioStartRequest,
    RealtimeTextRequest,
)
from application.realtime import (
    RealtimeAgentService,
    RealtimeAgentSession,
    RealtimeAudioChunkError,
    RealtimeSessionStateError,
)
from domain.agent import AgentDefinition
from domain.conversations import ConversationKey
from domain.realtime import RealtimeAudioDelta

logger = structlog.get_logger(__name__)

RealtimeControl = Annotated[
    RealtimeAudioStartRequest | RealtimeAudioEndRequest | RealtimeTextRequest,
    Field(discriminator="type"),
]
realtime_control_adapter: TypeAdapter[RealtimeControl] = TypeAdapter(RealtimeControl)


class InvalidRealtimeMessageError(ValueError):
    """Signal that a realtime JSON control frame is malformed or unsupported."""


async def serve_realtime_websocket(
    websocket: WebSocket,
    service: RealtimeAgentService,
    definition: AgentDefinition,
    conversation_key: ConversationKey,
) -> None:
    """Bridge one client socket and one provider session with bounded media flow."""
    send_lock = asyncio.Lock()
    try:
        async with service.open_session(definition, conversation_key) as session:
            await _send_json(
                websocket,
                send_lock,
                {
                    "type": "realtime_ready",
                    "data": {
                        "input_audio": "audio/pcm;rate=16000",
                        "output_audio": "audio/pcm;rate=24000",
                        "binary_audio_frames": True,
                    },
                },
            )
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    _receive_realtime_input(websocket, session, send_lock),
                    name=f"realtime-input-{conversation_key.conversation_id}",
                )
                tasks.create_task(
                    _send_realtime_output(websocket, session, send_lock),
                    name=f"realtime-output-{conversation_key.conversation_id}",
                )
    except* WebSocketDisconnect:
        logger.info("realtime_websocket_disconnected")
    except* Exception as group:
        logger.exception(
            "realtime_websocket_failed",
            error_types=[type(exc).__name__ for exc in group.exceptions],
        )
        await _send_error_safely(
            websocket,
            send_lock,
            code="realtime_session_failed",
            message="The realtime session could not continue.",
        )


async def _receive_realtime_input(
    websocket: WebSocket,
    session: RealtimeAgentSession,
    send_lock: asyncio.Lock,
) -> None:
    """Validate control or raw PCM frames and apply natural upstream backpressure."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(
                code=message.get("code", 1000),
                reason=message.get("reason"),
            )
        binary = message.get("bytes")
        try:
            if binary is not None:
                await session.send_audio(binary)
                continue
            text = message.get("text")
            if text is None:
                raise InvalidRealtimeMessageError
            control = _parse_control(text)
            if isinstance(control, RealtimeAudioStartRequest):
                turn_id = str(control.turn_id)
                await session.start_audio(turn_id)
                await _send_json(
                    websocket,
                    send_lock,
                    {"type": "audio_started", "data": {"turn_id": turn_id}},
                )
            elif isinstance(control, RealtimeAudioEndRequest):
                await session.end_audio()
                await _send_json(websocket, send_lock, {"type": "audio_ended", "data": {}})
            else:
                await session.send_text(str(control.turn_id), control.text)
        except (
            InvalidRealtimeMessageError,
            RealtimeAudioChunkError,
            RealtimeSessionStateError,
        ) as exc:
            await _send_json(
                websocket,
                send_lock,
                {
                    "type": "error",
                    "data": {"code": "invalid_realtime_input", "message": str(exc)},
                },
            )


def _parse_control(text: str) -> RealtimeControl:
    """Parse one discriminated JSON control without accepting unknown fields."""
    try:
        return realtime_control_adapter.validate_json(text)
    except ValidationError as exc:
        raise InvalidRealtimeMessageError("Invalid realtime control frame") from exc


async def _send_realtime_output(
    websocket: WebSocket,
    session: RealtimeAgentSession,
    send_lock: asyncio.Lock,
) -> None:
    """Forward audio as raw binary and semantic events as JSON without buffering."""
    async for event in session.events():
        if isinstance(event, RealtimeAudioDelta):
            async with send_lock:
                await websocket.send_bytes(event.data)
            continue
        event_type, data = realtime_event_payload(event)
        await _send_json(websocket, send_lock, {"type": event_type, "data": data})


async def _send_error_safely(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    *,
    code: str,
    message: str,
) -> None:
    """Best-effort reporting at the transport boundary after provider failure."""
    try:
        await _send_json(
            websocket,
            send_lock,
            {"type": "error", "data": {"code": code, "message": message}},
        )
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _send_json(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    payload: dict[str, object],
) -> None:
    """Serialize concurrent JSON and binary writes over one WebSocket."""
    async with send_lock:
        await websocket.send_json(payload)
