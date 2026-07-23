from typing import Annotated, Literal, cast
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

from api.realtime_websocket import serve_realtime_websocket
from api.schemas import (
    ConversationGroupResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    StreamAgentRequest,
)
from api.sse import encode_agent_stream
from api.websocket import serve_agent_websocket
from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
    ConversationHistoryService,
    ConversationNotFoundError,
    ConversationService,
)
from application.interactions import ConversationCoordinator, InteractionQueueFullError
from application.realtime import RealtimeAgentService
from bootstrap import AppContainer
from domain.conversations import ConversationKey
from domain.realtime import RealtimeActivityConfig, RealtimeSessionOptions

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


def get_conversation_history_service(
    container: Annotated[AppContainer, Depends(get_container)],
) -> ConversationHistoryService:
    """Resolve canonical conversation inspection operations."""
    return container.conversation_history_service


def get_realtime_agent_service(
    container: Annotated[AppContainer, Depends(get_container)],
) -> RealtimeAgentService:
    """Resolve the always-composed full-duplex service."""
    return container.realtime_agent_service


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
    conversation = await service.create_session(payload.user_id)
    return CreateSessionResponse(session_uid=conversation.key.conversation_id)


@router.get(
    "/v1/sessions",
    response_model=ConversationListResponse,
    tags=["agent"],
)
async def list_sessions(
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    service: Annotated[
        ConversationHistoryService,
        Depends(get_conversation_history_service),
    ],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationListResponse:
    """List persisted sessions so technical clients can select one for inspection."""
    page = await service.list_sessions(user_id, offset=offset, limit=limit)
    return ConversationListResponse.from_page(page, user_id=user_id, offset=offset)


@router.get(
    "/v1/sessions/{session_uid}/history",
    response_model=ConversationHistoryResponse,
    tags=["agent"],
)
async def get_session_history(
    session_uid: Annotated[UUID, Path()],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    service: Annotated[
        ConversationHistoryService,
        Depends(get_conversation_history_service),
    ],
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationHistoryResponse:
    """Return canonical database records, including matched tool calls and results."""
    key = ConversationKey(conversation_id=str(session_uid), user_id=user_id)
    try:
        history = await service.load(
            key,
            after_sequence=after_sequence,
            limit=limit,
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    return ConversationHistoryResponse.from_history(history)


@router.get(
    "/v1/sessions/{session_uid}/group",
    response_model=ConversationGroupResponse,
    tags=["agent"],
)
async def get_session_group(
    session_uid: Annotated[UUID, Path()],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    service: Annotated[
        ConversationHistoryService,
        Depends(get_conversation_history_service),
    ],
) -> ConversationGroupResponse:
    """Return one root conversation and its isolated A2A worker sessions."""
    key = ConversationKey(conversation_id=str(session_uid), user_id=user_id)
    try:
        group = await service.load_group(key)
    except ConversationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    return ConversationGroupResponse.from_group(group)


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
) -> None:
    """Keep an owned conversation open and stream each correlated turn as JSON."""
    key = ConversationKey(
        conversation_id=str(session_uid),
        user_id=user_id,
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


@router.websocket("/v1/agent/realtime")
async def realtime_agent_websocket(
    websocket: WebSocket,
    session_uid: Annotated[UUID, Query()],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    service: Annotated[RealtimeAgentService, Depends(get_realtime_agent_service)],
    container: Annotated[AppContainer, Depends(get_container)],
    conversation_service: Annotated[ConversationService, Depends(get_conversation_service)],
    activity_detection: Annotated[Literal["automatic", "explicit"], Query()] = "automatic",
    start_sensitivity: Annotated[Literal["high", "low"] | None, Query()] = None,
    end_sensitivity: Annotated[Literal["high", "low"] | None, Query()] = None,
    prefix_padding_ms: Annotated[int | None, Query(ge=0, le=10_000)] = None,
    silence_duration_ms: Annotated[int | None, Query(ge=0, le=60_000)] = None,
    interrupt_on_activity: Annotated[bool, Query()] = True,
) -> None:
    """Bridge raw PCM frames to a configured full-duplex provider session."""
    key = ConversationKey(
        conversation_id=str(session_uid),
        user_id=user_id,
    )
    options = RealtimeSessionOptions(
        activity=RealtimeActivityConfig(
            detection=activity_detection,
            start_sensitivity=start_sensitivity,
            end_sensitivity=end_sensitivity,
            prefix_padding_ms=prefix_padding_ms,
            silence_duration_ms=silence_duration_ms,
            interrupt_on_activity=interrupt_on_activity,
        )
    )
    if activity_detection not in service.capabilities.activity_detection_modes:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Requested activity detection mode is not supported",
        )
    if interrupt_on_activity and not service.capabilities.supports_barge_in:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Requested barge-in behavior is not supported",
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
                "flow": "speech_to_speech",
            },
        }
    )
    logger.info("realtime_websocket_connected")
    await serve_realtime_websocket(
        websocket,
        service,
        container.realtime_definition,
        key,
        options,
    )


@router.delete("/v1/conversations/{conversation_id}", tags=["agent"])
async def delete_conversation(
    conversation_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> dict[str, bool]:
    """Delete retained conversation data for its owning security principal."""
    key = ConversationKey(conversation_id=conversation_id, user_id=user_id)
    try:
        deleted = await service.delete(key)
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    except ConversationConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Conversation was updated concurrently"
        ) from exc
    return {"deleted": deleted}
