import asyncio
from uuid import UUID

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from api.event_payloads import agent_event_payload
from api.schemas import AgentWebSocketRequest
from application.interactions import ConversationCoordinator, InteractionQueueFullError
from domain.conversations import ConversationKey

logger = structlog.get_logger(__name__)


class InvalidWebSocketMessageError(ValueError):
    """Signal that an incoming frame is not a valid agent request."""


async def serve_agent_websocket(
    websocket: WebSocket,
    coordinator: ConversationCoordinator,
    conversation_key: ConversationKey,
) -> None:
    """Enqueue inputs and deliver durable outputs over one persistent socket."""
    send_lock = asyncio.Lock()
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                _receive_requests(websocket, coordinator, conversation_key, send_lock),
                name=f"agent-ws-receiver-{conversation_key.conversation_id}",
            )
            tasks.create_task(
                _send_pending_outputs(websocket, coordinator, conversation_key, send_lock),
                name=f"agent-ws-sender-{conversation_key.conversation_id}",
            )
    except* WebSocketDisconnect:
        logger.info("agent_websocket_disconnected")


async def _receive_requests(
    websocket: WebSocket,
    coordinator: ConversationCoordinator,
    conversation_key: ConversationKey,
    send_lock: asyncio.Lock,
) -> None:
    """Validate frames and persist them without coupling socket life to execution."""
    while True:
        try:
            request = await _receive_request(websocket)
        except InvalidWebSocketMessageError:
            await _send_error(
                websocket,
                send_lock,
                request_id=None,
                code="invalid_message",
                message="Expected a valid JSON message frame.",
            )
            continue
        try:
            await coordinator.submit(
                request.message,
                conversation_key,
                request_id=str(request.request_id),
                source="text_user",
            )
        except InteractionQueueFullError:
            await _send_error(
                websocket,
                send_lock,
                request_id=request.request_id,
                code="too_many_pending_messages",
                message="Wait for pending agent turns to complete before sending more.",
            )


async def _receive_request(websocket: WebSocket) -> AgentWebSocketRequest:
    """Read one text frame and parse the public request schema."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(
            code=message.get("code", 1000),
            reason=message.get("reason"),
        )
    text = message.get("text")
    if text is None:
        raise InvalidWebSocketMessageError
    try:
        return AgentWebSocketRequest.model_validate_json(text)
    except ValidationError as exc:
        raise InvalidWebSocketMessageError from exc


async def _send_pending_outputs(
    websocket: WebSocket,
    coordinator: ConversationCoordinator,
    conversation_key: ConversationKey,
    send_lock: asyncio.Lock,
) -> None:
    """Deliver live and offline-completed events until the socket disconnects."""
    async for output in coordinator.stream_pending_outputs(conversation_key):
        event_type, data = agent_event_payload(output.event)
        await _send_json(
            websocket,
            send_lock,
            {
                "type": event_type,
                "request_id": output.request_id,
                "data": data,
            },
        )


async def _send_error(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    *,
    request_id: UUID | None,
    code: str,
    message: str,
) -> None:
    """Send a safe error envelope without exposing internal exception details."""
    await _send_json(
        websocket,
        send_lock,
        {
            "type": "error",
            "request_id": str(request_id) if request_id is not None else None,
            "data": {"code": code, "message": message},
        },
    )


async def _send_json(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    payload: dict[str, object],
) -> None:
    """Serialize concurrent socket producers into atomic JSON sends."""
    async with send_lock:
        await websocket.send_json(payload)
