from collections.abc import AsyncIterator
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.middleware import request_logging_middleware
from api.routes import router
from application.conversations import ConversationNotFoundError
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import Conversation, ConversationKey
from domain.events import AgentStreamCompleted, AgentStreamEvent, AgentTextDelta

SESSION_UID = "12345678-1234-4678-9234-567812345678"


class StubAgentService:
    """Stream a deterministic response without managing conversation lifecycle."""

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        key: ConversationKey,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Emit one text delta and terminal event for the expected session."""
        assert message == "Hola"
        assert definition.model == "test-model"
        assert key == ConversationKey(conversation_id=SESSION_UID, user_id="user-1")
        yield AgentTextDelta(text="Hola")
        yield AgentStreamCompleted(
            result=AgentResult(
                answer="Hola, ¿en qué puedo ayudarte?",
                response_id="resp_stream",
                conversation_id=key.conversation_id,
            )
        )


class StubConversationService:
    """Manage one deterministic session for API boundary tests."""

    async def create_session(self, user_id: str, tenant_id: str | None = None) -> Conversation:
        """Return the deterministic empty session."""
        assert user_id == "user-1"
        assert tenant_id is None
        return Conversation(
            key=ConversationKey(
                conversation_id=SESSION_UID,
                user_id=user_id,
                tenant_id=tenant_id,
            ),
            title="Nueva conversación",
        )

    async def require(self, key: ConversationKey) -> Conversation:
        """Reject every UID except the deterministic existing session."""
        if key == ConversationKey(conversation_id=SESSION_UID, user_id="user-1"):
            return Conversation(key=key)
        raise ConversationNotFoundError

    async def delete(self, key: ConversationKey) -> bool:
        """Report deletion of the deterministic session."""
        assert key == ConversationKey(conversation_id=SESSION_UID, user_id="user-1")
        return True


def build_test_app() -> FastAPI:
    """Build the API with separate agent and conversation service doubles."""
    app = FastAPI()
    app.state.container = SimpleNamespace(
        agent_service=StubAgentService(),
        conversation_service=StubConversationService(),
        default_agent=AgentDefinition(
            model="test-model",
            instructions="Test",
            tool_names=(),
        ),
    )
    app.middleware("http")(request_logging_middleware)
    app.include_router(router)
    return app


async def test_session_creation_and_agent_stream() -> None:
    """Create a session and use its UID in the only model-facing endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=build_test_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        create_response = await client.post("/v1/sessions", json={"user_id": "user-1"})
        stream_response = await client.post(
            "/v1/agent/stream",
            json={"message": "Hola", "session_uid": SESSION_UID, "user_id": "user-1"},
        )
        delete_response = await client.delete(f"/v1/conversations/{SESSION_UID}?user_id=user-1")
        removed_run = await client.post("/v1/agent/run", json={})
        removed_chat = await client.post("/v1/chat", json={})

    assert create_response.status_code == 201
    assert create_response.json() == {"session_uid": SESSION_UID}
    assert stream_response.status_code == 200
    assert stream_response.headers["content-type"].startswith("text/event-stream")
    assert 'event: text_delta\ndata: {"text":"Hola"}\n\n' in stream_response.text
    assert '"session_uid":"12345678-1234-4678-9234-567812345678"' in stream_response.text
    assert delete_response.json() == {"deleted": True}
    assert removed_run.status_code == 404
    assert removed_chat.status_code == 404


async def test_stream_rejects_an_unknown_session_before_starting_sse() -> None:
    """Return HTTP 404 before opening a stream for an unknown session UID."""
    async with AsyncClient(
        transport=ASGITransport(app=build_test_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/stream",
            json={
                "message": "Hola",
                "session_uid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "user_id": "user-1",
            },
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Session not found"}
