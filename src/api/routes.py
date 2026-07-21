from typing import Annotated, cast
from uuid import UUID, uuid4

import structlog
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.responses import StreamingResponse
from starlette.requests import HTTPConnection

from api.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    StreamAgentRequest,
)
from api.sse import encode_agent_stream
from api.websocket import serve_agent_websocket
from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationService,
)
from application.interactions import ConversationCoordinator, InteractionQueueFullError
from bootstrap import AppContainer
from domain.conversations import ConversationKey

router = APIRouter()
logger = structlog.get_logger(__name__)


def get_container(connection: HTTPConnection) -> AppContainer:
    """Return the application container for an HTTP or WebSocket connection."""
    return cast(AppContainer, connection.app.state.container)


def get_conversation_coordinator(
    container: Annotated[AppContainer, Depends(get_container)],
) -> ConversationCoordinator:
    """Resolve the durable, modality-neutral interaction coordinator."""
    return container.conversation_coordinator


def get_conversation_service(
    container: Annotated[AppContainer, Depends(get_container)],
) -> ConversationService:
    """Resolve conversation lifecycle operations for an HTTP request."""
    return container.conversation_service


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Report that the HTTP process is alive without calling external services."""
    return {"status": "ok"}


@router.post(
    "/v1/sessions",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
)
async def create_session(
    payload: CreateSessionRequest,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> CreateSessionResponse:
    """Create an empty persisted chat session and return its generated UID."""
    conversation = await service.create_session(payload.user_id, payload.tenant_id)
    return CreateSessionResponse(session_uid=conversation.key.conversation_id)


@router.post("/v1/agent/stream", response_class=StreamingResponse, tags=["agent"])
async def stream_agent(
    payload: StreamAgentRequest,
    coordinator: Annotated[
        ConversationCoordinator,
        Depends(get_conversation_coordinator),
    ],
    conversation_service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> StreamingResponse:
    """Validate, stream, and persist one interaction for an existing session."""
    key = ConversationKey(
        conversation_id=str(payload.session_uid),
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
    )
    try:
        await conversation_service.require(key)
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    try:
        command = await coordinator.submit(
            payload.message,
            key,
            request_id=str(uuid4()),
            source="text_user",
        )
    except InteractionQueueFullError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many pending conversation messages",
        ) from exc
    events = encode_agent_stream(
        output.event async for output in coordinator.stream_command_outputs(command)
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.websocket("/v1/agent/ws")
async def agent_websocket(
    websocket: WebSocket,
    session_uid: Annotated[UUID, Query()],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    coordinator: Annotated[
        ConversationCoordinator,
        Depends(get_conversation_coordinator),
    ],
    conversation_service: Annotated[ConversationService, Depends(get_conversation_service)],
    tenant_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> None:
    """Keep an owned conversation open and stream each correlated turn as JSON."""
    key = ConversationKey(
        conversation_id=str(session_uid),
        user_id=user_id,
        tenant_id=tenant_id,
    )
    try:
        await conversation_service.require(key)
    except ConversationNotFoundError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Session not found",
        ) from exc
    except ConversationAccessDeniedError as exc:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Conversation access denied",
        ) from exc

    connection_id = websocket.headers.get("x-request-id") or str(uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        connection_id=connection_id,
        conversation_id=key.conversation_id,
    )
    await websocket.accept()
    await websocket.send_json(
        {
            "type": "connected",
            "data": {
                "connection_id": connection_id,
                "session_uid": str(session_uid),
            },
        }
    )
    logger.info("agent_websocket_connected")
    await serve_agent_websocket(websocket, coordinator, key)


@router.delete("/v1/conversations/{conversation_id}", tags=["agent"])
async def delete_conversation(
    conversation_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    tenant_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, bool]:
    """Delete retained conversation data for its owning security principal."""
    key = ConversationKey(conversation_id=conversation_id, user_id=user_id, tenant_id=tenant_id)
    try:
        deleted = await service.delete(key)
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    except ConversationConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Conversation was updated concurrently"
        ) from exc
    return {"deleted": deleted}
