from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse

from api.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    StreamAgentRequest,
)
from api.sse import encode_agent_stream
from application.agent import AgentService
from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationService,
)
from bootstrap import AppContainer
from domain.agent import AgentDefinition
from domain.conversations import ConversationKey

router = APIRouter()


def get_container(request: Request) -> AppContainer:
    """Return the application container attached during FastAPI startup."""
    return cast(AppContainer, request.app.state.container)


def get_agent_service(
    container: Annotated[AppContainer, Depends(get_container)],
) -> AgentService:
    """Resolve the shared agent orchestrator for an HTTP request."""
    return container.agent_service


def get_agent_definition(
    container: Annotated[AppContainer, Depends(get_container)],
) -> AgentDefinition:
    """Resolve the immutable default agent configuration used by the endpoint."""
    return container.default_agent


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
    agent_service: Annotated[AgentService, Depends(get_agent_service)],
    conversation_service: Annotated[ConversationService, Depends(get_conversation_service)],
    definition: Annotated[AgentDefinition, Depends(get_agent_definition)],
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
    events = encode_agent_stream(agent_service.stream(payload.message, definition, key))
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


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
