import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from uuid import UUID

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from api.event_payloads import agent_event_payload
from api.schemas import AgentWebSocketRequest
from application.agent import AgentService
from domain.agent import AgentDefinition
from domain.conversations import ConversationKey
from domain.events import AgentStreamEvent

logger = structlog.get_logger(__name__)

MAX_PENDING_MESSAGES = 8


class InvalidWebSocketMessageError(ValueError):
    """Signal that an incoming frame is not a valid agent request."""


async def serve_agent_websocket(
    websocket: WebSocket,
    agent_service: AgentService,
    definition: AgentDefinition,
    conversation_key: ConversationKey,
) -> None:
    """Receive turns and stream correlated agent events over one persistent socket."""
    requests: asyncio.Queue[AgentWebSocketRequest] = asyncio.Queue(maxsize=MAX_PENDING_MESSAGES)
    send_lock = asyncio.Lock()
    disconnected = asyncio.Event()
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                _receive_requests(websocket, requests, send_lock, disconnected),
                name=f"agent-ws-receiver-{conversation_key.conversation_id}",
            )
            tasks.create_task(
                _process_requests(
                    websocket,
                    requests,
                    send_lock,
                    disconnected,
                    agent_service,
                    definition,
                    conversation_key,
                ),
                name=f"agent-ws-processor-{conversation_key.conversation_id}",
            )
    except* WebSocketDisconnect:
        logger.info("agent_websocket_disconnected_during_send")


async def _receive_requests(
    websocket: WebSocket,
    requests: asyncio.Queue[AgentWebSocketRequest],
    send_lock: asyncio.Lock,
    disconnected: asyncio.Event,
) -> None:
    """Validate incoming text frames without building an unbounded request buffer."""
    try:
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
                requests.put_nowait(request)
            except asyncio.QueueFull:
                await _send_error(
                    websocket,
                    send_lock,
                    request_id=request.request_id,
                    code="too_many_pending_messages",
                    message="Wait for pending agent turns to complete before sending more.",
                )
    except WebSocketDisconnect as exc:
        logger.info("agent_websocket_disconnected", close_code=exc.code)
    finally:
        disconnected.set()


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


async def _process_requests(
    websocket: WebSocket,
    requests: asyncio.Queue[AgentWebSocketRequest],
    send_lock: asyncio.Lock,
    disconnected: asyncio.Event,
    agent_service: AgentService,
    definition: AgentDefinition,
    conversation_key: ConversationKey,
) -> None:
    """Run accepted turns sequentially so conversation updates preserve their order."""
    while True:
        request = await _next_request(requests, disconnected)
        if request is None:
            return
        request_id = str(request.request_id)
        try:
            with structlog.contextvars.bound_contextvars(request_id=request_id):
                completed = await _stream_turn_until_disconnected(
                    websocket,
                    send_lock,
                    request,
                    disconnected,
                    agent_service,
                    definition,
                    conversation_key,
                )
                if not completed:
                    return
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            logger.exception("agent_websocket_turn_failed", error_type=type(exc).__name__)
            await _send_error(
                websocket,
                send_lock,
                request_id=request.request_id,
                code="agent_error",
                message="The agent turn could not be completed.",
            )
        finally:
            requests.task_done()


async def _next_request(
    requests: asyncio.Queue[AgentWebSocketRequest],
    disconnected: asyncio.Event,
) -> AgentWebSocketRequest | None:
    """Wait for a queued turn while allowing a disconnect to stop the processor."""
    request_task = asyncio.create_task(requests.get())
    disconnect_task = asyncio.create_task(disconnected.wait())
    try:
        await asyncio.wait(
            {request_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnected.is_set():
            return None
        return request_task.result()
    finally:
        for task in (request_task, disconnect_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(request_task, disconnect_task, return_exceptions=True)


async def _stream_turn_until_disconnected(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    request: AgentWebSocketRequest,
    disconnected: asyncio.Event,
    agent_service: AgentService,
    definition: AgentDefinition,
    conversation_key: ConversationKey,
) -> bool:
    """Cancel an active model stream promptly when the socket reader disconnects."""
    turn_task = asyncio.create_task(
        _stream_turn(
            websocket,
            send_lock,
            request,
            agent_service,
            definition,
            conversation_key,
        )
    )
    disconnect_task = asyncio.create_task(disconnected.wait())
    try:
        await asyncio.wait(
            {turn_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnected.is_set():
            return False
        await turn_task
        return True
    finally:
        for task in (turn_task, disconnect_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(turn_task, disconnect_task, return_exceptions=True)


async def _stream_turn(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    request: AgentWebSocketRequest,
    agent_service: AgentService,
    definition: AgentDefinition,
    conversation_key: ConversationKey,
) -> None:
    """Forward one application event stream and close it on cancellation."""
    events: AsyncGenerator[AgentStreamEvent, None] = agent_service.stream(
        request.message, definition, conversation_key
    )
    async with aclosing(events):
        async for event in events:
            event_type, data = agent_event_payload(event)
            await _send_json(
                websocket,
                send_lock,
                {
                    "type": event_type,
                    "request_id": str(request.request_id),
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
