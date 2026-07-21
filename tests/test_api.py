import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from api.middleware import request_logging_middleware
from api.routes import router
from application.conversations import ConversationNotFoundError
from domain.agent import AgentResult
from domain.conversations import Conversation, ConversationKey
from domain.events import AgentStreamCompleted, AgentTextDelta
from domain.interactions import InteractionCommand, InteractionOutput

SESSION_UID = "12345678-1234-4678-9234-567812345678"
REQUEST_UID = "87654321-4321-4765-8321-876543218765"
SECOND_REQUEST_UID = "aaaaaaaa-4321-4765-8321-876543218765"


class StubConversationCoordinator:
    """Expose deterministic durable commands and output streams to API tests."""

    def __init__(self) -> None:
        """Initialize submitted commands and the live-delivery queue."""
        self.commands: list[InteractionCommand] = []
        self.pending: asyncio.Queue[InteractionOutput] = asyncio.Queue()

    async def submit(
        self,
        message: str,
        conversation: ConversationKey,
        *,
        request_id: str,
        source: str,
    ) -> InteractionCommand:
        """Capture one text command and make its outputs available to WebSocket."""
        assert message == "Hola"
        assert conversation == ConversationKey(conversation_id=SESSION_UID, user_id="user-1")
        assert source == "text_user"
        command = InteractionCommand(
            command_id=str(uuid4()),
            request_id=request_id,
            conversation=conversation,
            kind="user_message",
            source="text_user",
            message=message,
        )
        self.commands.append(command)
        for output in self._outputs(command):
            await self.pending.put(output)
        return command

    async def stream_command_outputs(
        self,
        command: InteractionCommand,
    ) -> AsyncIterator[InteractionOutput]:
        """Emit deterministic SSE outputs for one submitted command."""
        for output in self._outputs(command):
            yield output

    async def stream_pending_outputs(
        self,
        conversation: ConversationKey,
    ) -> AsyncIterator[InteractionOutput]:
        """Emit outputs submitted while a WebSocket listener is active."""
        while True:
            output = await self.pending.get()
            assert output.conversation == conversation
            yield output

    @staticmethod
    def _outputs(command: InteractionCommand) -> tuple[InteractionOutput, ...]:
        """Build one delta and terminal output correlated to a command."""
        completed = AgentStreamCompleted(
            result=AgentResult(
                answer="Hola, ¿en qué puedo ayudarte?",
                response_id="resp_stream",
                conversation_id=command.conversation.conversation_id,
            )
        )
        return (
            InteractionOutput(
                output_id=f"{command.command_id}:0",
                command_id=command.command_id,
                request_id=command.request_id,
                conversation=command.conversation,
                modality="text",
                event=AgentTextDelta(text="Hola"),
                sequence=1,
            ),
            InteractionOutput(
                output_id=f"{command.command_id}:1",
                command_id=command.command_id,
                request_id=command.request_id,
                conversation=command.conversation,
                modality="text",
                event=completed,
                sequence=2,
            ),
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


def build_test_app(
    coordinator: StubConversationCoordinator | None = None,
) -> FastAPI:
    """Build the API with coordinator and conversation lifecycle doubles."""
    app = FastAPI()
    app.state.container = SimpleNamespace(
        conversation_coordinator=coordinator or StubConversationCoordinator(),
        conversation_service=StubConversationService(),
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


def test_agent_websocket_disconnect_does_not_discard_the_submitted_command() -> None:
    """Keep accepted work durable when its originating socket disconnects."""
    coordinator = StubConversationCoordinator()
    with TestClient(build_test_app(coordinator)) as client:
        with client.websocket_connect(
            f"/v1/agent/ws?session_uid={SESSION_UID}&user_id=user-1"
        ) as websocket:
            websocket.receive_json()
            websocket.send_json({"type": "message", "message": "Hola"})
            assert websocket.receive_json()["data"] == {"text": "Hola"}

    assert len(coordinator.commands) == 1


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
