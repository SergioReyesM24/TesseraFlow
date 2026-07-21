import asyncio
from collections.abc import AsyncIterator
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from api.middleware import request_logging_middleware
from api.routes import router
from application.conversations import ConversationNotFoundError
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import Conversation, ConversationKey
from domain.events import AgentStreamCompleted, AgentStreamEvent, AgentTextDelta

SESSION_UID = "12345678-1234-4678-9234-567812345678"
REQUEST_UID = "87654321-4321-4765-8321-876543218765"
SECOND_REQUEST_UID = "aaaaaaaa-4321-4765-8321-876543218765"


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


class CancellableAgentService:
    """Expose when a long-running model stream is closed by the transport."""

    def __init__(self) -> None:
        """Create a thread-safe cancellation observation flag."""
        self.closed = Event()

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        key: ConversationKey,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Emit one delta and then remain active until the socket cancels the turn."""
        del message, definition, key
        try:
            yield AgentTextDelta(text="Trabajando")
            await asyncio.Event().wait()
        finally:
            self.closed.set()


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


def build_test_app(
    agent_service: StubAgentService | CancellableAgentService | None = None,
) -> FastAPI:
    """Build the API with separate agent and conversation service doubles."""
    app = FastAPI()
    app.state.container = SimpleNamespace(
        agent_service=agent_service or StubAgentService(),
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
    """Keep the previous SSE endpoint available during the WebSocket migration."""
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


def test_agent_websocket_streams_correlated_json_events() -> None:
    """Stream a complete agent turn as JSON over a persistent WebSocket."""
    with TestClient(build_test_app()) as client:
        with client.websocket_connect(
            f"/v1/agent/ws?session_uid={SESSION_UID}&user_id=user-1"
        ) as websocket:
            connected = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "message",
                    "request_id": REQUEST_UID,
                    "message": "Hola",
                }
            )
            delta = websocket.receive_json()
            completed = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "message",
                    "request_id": SECOND_REQUEST_UID,
                    "message": "Hola",
                }
            )
            second_delta = websocket.receive_json()
            second_completed = websocket.receive_json()

    assert connected["type"] == "connected"
    assert connected["data"]["session_uid"] == SESSION_UID
    assert connected["data"]["connection_id"]
    assert delta == {
        "type": "text_delta",
        "request_id": REQUEST_UID,
        "data": {"text": "Hola"},
    }
    assert completed["type"] == "completed"
    assert completed["request_id"] == REQUEST_UID
    assert completed["data"]["answer"] == "Hola, ¿en qué puedo ayudarte?"
    assert completed["data"]["session_uid"] == SESSION_UID
    assert second_delta["request_id"] == SECOND_REQUEST_UID
    assert second_delta["data"] == {"text": "Hola"}
    assert second_completed["type"] == "completed"
    assert second_completed["request_id"] == SECOND_REQUEST_UID


def test_agent_websocket_recovers_after_an_invalid_message() -> None:
    """Reject a malformed frame without closing the conversation socket."""
    with TestClient(build_test_app()) as client:
        with client.websocket_connect(
            f"/v1/agent/ws?session_uid={SESSION_UID}&user_id=user-1"
        ) as websocket:
            websocket.receive_json()
            websocket.send_text("not-json")
            error = websocket.receive_json()
            websocket.send_json({"type": "message", "message": "Hola"})
            delta = websocket.receive_json()

    assert error == {
        "type": "error",
        "request_id": None,
        "data": {
            "code": "invalid_message",
            "message": "Expected a valid JSON message frame.",
        },
    }
    assert delta["type"] == "text_delta"
    assert delta["data"] == {"text": "Hola"}


def test_agent_websocket_rejects_an_unknown_session() -> None:
    """Reject the WebSocket handshake when the owned session does not exist."""
    with TestClient(build_test_app()) as client:
        with pytest.raises(WebSocketDisconnect) as raised:
            with client.websocket_connect(
                "/v1/agent/ws?session_uid=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa&user_id=user-1"
            ):
                pass

    assert raised.value.code == 1008
    assert raised.value.reason == "Session not found"


def test_agent_websocket_disconnect_cancels_the_active_stream() -> None:
    """Close the active model stream as soon as the socket reader disconnects."""
    agent_service = CancellableAgentService()
    with TestClient(build_test_app(agent_service)) as client:
        with client.websocket_connect(
            f"/v1/agent/ws?session_uid={SESSION_UID}&user_id=user-1"
        ) as websocket:
            websocket.receive_json()
            websocket.send_json({"type": "message", "message": "Hola"})
            assert websocket.receive_json()["data"] == {"text": "Trabajando"}

    assert agent_service.closed.wait(timeout=1)


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
