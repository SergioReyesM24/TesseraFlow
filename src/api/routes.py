from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse

from api.schemas import RunAgentRequest, RunAgentResponse
from api.sse import encode_agent_stream
from application.agent import AgentService
from application.conversations import (
    ConversationAccessDeniedError,
    ConversationConflictError,
    ConversationTooLargeError,
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


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    """Report that the HTTP process is alive without calling external services."""
    return {"status": "ok"}


@router.post("/v1/agent/run", response_model=RunAgentResponse, tags=["agent"])
async def run_agent(
    payload: RunAgentRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
    definition: Annotated[AgentDefinition, Depends(get_agent_definition)],
) -> RunAgentResponse:
    """Execute and persist one owned conversation interaction."""
    key = ConversationKey(
        conversation_id=payload.conversation_id,
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
    )
    try:
        result = await service.run(payload.message, definition, key)
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    except ConversationConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Conversation was updated concurrently"
        ) from exc
    except ConversationTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    return RunAgentResponse.from_result(result)


@router.post("/v1/agent/stream", response_class=StreamingResponse, tags=["agent"])
async def stream_agent(
    payload: RunAgentRequest,
    service: Annotated[AgentService, Depends(get_agent_service)],
    definition: Annotated[AgentDefinition, Depends(get_agent_definition)],
) -> StreamingResponse:
    """Stream and persist one owned conversation interaction using SSE."""
    key = ConversationKey(
        conversation_id=payload.conversation_id,
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
    )
    events = encode_agent_stream(service.stream(payload.message, definition, key))
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
    service: Annotated[AgentService, Depends(get_agent_service)],
    user_id: Annotated[str, Query(min_length=1, max_length=128)],
    tenant_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, bool]:
    """Delete retained conversation data for its owning security principal."""
    key = ConversationKey(conversation_id=conversation_id, user_id=user_id, tenant_id=tenant_id)
    try:
        deleted = await service.delete_conversation(key)
    except ConversationAccessDeniedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conversation access denied") from exc
    except ConversationConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Conversation was updated concurrently"
        ) from exc
    return {"deleted": deleted}
