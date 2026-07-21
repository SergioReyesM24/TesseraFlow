import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import Conversation, ConversationKey
from domain.interactions import InteractionCommand, InteractionOutput
from domain.realtime import (
    RealtimeAgentEvent,
    RealtimeAudioDelta,
    RealtimeOutputTranscriptDelta,
    RealtimeTurnCompleted,
)
from domain.turn_events import AgentStreamCompleted, AgentTextDelta

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

    async def create_session(self, user_id: str) -> Conversation:
        """Return the deterministic empty session."""
        assert user_id == "user-1"
        return Conversation(
            key=ConversationKey(
                conversation_id=SESSION_UID,
                user_id=user_id,
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


class StubRealtimeSession:
    """Capture realtime WebSocket input and emit deterministic binary output."""

    def __init__(self) -> None:
        """Initialize media captures and an asynchronous event queue."""
        self.audio: list[bytes] = []
        self.started_turns: list[str] = []
        self.audio_end_count = 0
        self.pending: asyncio.Queue[RealtimeAgentEvent] = asyncio.Queue()

    async def start_audio(self, turn_id: str) -> None:
        """Capture one logical audio start boundary."""
        self.started_turns.append(turn_id)

    async def send_audio(self, data: bytes) -> None:
        """Capture one raw binary client frame."""
        self.audio.append(data)

    async def end_audio(self) -> None:
        """Capture one paused input stream."""
        self.audio_end_count += 1

    async def send_text(self, turn_id: str, text: str) -> None:
        """Emit transcript, raw audio, and completion for one fallback turn."""
        assert text == "Hola realtime"
        await self.pending.put(RealtimeOutputTranscriptDelta(turn_id=turn_id, text="Respuesta"))
        await self.pending.put(
            RealtimeAudioDelta(
                turn_id=turn_id,
                data=b"\x01\x02",
                mime_type="audio/pcm;rate=24000",
            )
        )
        await self.pending.put(
            RealtimeTurnCompleted(
                turn_id=turn_id,
                result=AgentResult(
                    answer="Respuesta",
                    response_id="realtime-response",
                    conversation_id=SESSION_UID,
                ),
            )
        )

    async def events(self) -> AsyncIterator[RealtimeAgentEvent]:
        """Wait for and yield events until socket cancellation."""
        while True:
            yield await self.pending.get()


class StubRealtimeService:
    """Open one deterministic realtime application session."""

    def __init__(self) -> None:
        """Expose one reusable session for transport assertions."""
        self.session = StubRealtimeSession()

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
    ) -> AsyncIterator[StubRealtimeSession]:
        """Validate route composition and yield the transport double."""
        assert definition.model == "realtime-model"
        assert conversation_key == ConversationKey(
            conversation_id=SESSION_UID,
            user_id="user-1",
        )
        yield self.session


def build_test_app(
    coordinator: StubConversationCoordinator | None = None,
    realtime_service: StubRealtimeService | None = None,
) -> FastAPI:
    """Build the API with coordinator and conversation lifecycle doubles."""
    app = FastAPI()
    app.state.container = SimpleNamespace(
        conversation_coordinator=coordinator or StubConversationCoordinator(),
        conversation_service=StubConversationService(),
        realtime_agent_service=realtime_service,
        default_agent=AgentDefinition(
            model="realtime-model",
            instructions="Be helpful.",
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


def test_realtime_websocket_bridges_binary_pcm_and_semantic_events() -> None:
    """Keep PCM frames binary while control, transcripts, and completion remain JSON."""
    service = StubRealtimeService()
    with TestClient(build_test_app(realtime_service=service)) as client:
        with client.websocket_connect(
            f"/v1/agent/realtime?session_uid={SESSION_UID}&user_id=user-1"
        ) as websocket:
            connected = websocket.receive_json()
            ready = websocket.receive_json()
            websocket.send_json({"type": "audio_start", "turn_id": REQUEST_UID})
            started = websocket.receive_json()
            websocket.send_bytes(b"\x00\x00\x01\x00")
            websocket.send_json({"type": "audio_end"})
            ended = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "text",
                    "turn_id": SECOND_REQUEST_UID,
                    "text": "Hola realtime",
                }
            )
            transcript = websocket.receive_json()
            audio = websocket.receive_bytes()
            completed = websocket.receive_json()

    assert connected["type"] == "connected"
    assert connected["data"]["flow"] == "speech_to_speech"
    assert ready == {
        "type": "realtime_ready",
        "data": {
            "input_audio": "audio/pcm;rate=16000",
            "output_audio": "audio/pcm;rate=24000",
            "binary_audio_frames": True,
        },
    }
    assert started == {
        "type": "audio_started",
        "data": {"turn_id": REQUEST_UID},
    }
    assert ended == {"type": "audio_ended", "data": {}}
    assert service.session.audio == [b"\x00\x00\x01\x00"]
    assert service.session.audio_end_count == 1
    assert transcript == {
        "type": "output_transcript_delta",
        "data": {"turn_id": SECOND_REQUEST_UID, "text": "Respuesta"},
    }
    assert audio == b"\x01\x02"
    assert completed["type"] == "turn_completed"
    assert completed["data"]["turn_id"] == SECOND_REQUEST_UID
    assert completed["data"]["answer"] == "Respuesta"


def test_realtime_websocket_requires_speech_to_speech_configuration() -> None:
    """Reject the realtime handshake when no full-duplex service was composed."""
    with TestClient(build_test_app()) as client:
        with pytest.raises(WebSocketDisconnect) as raised:
            with client.websocket_connect(
                f"/v1/agent/realtime?session_uid={SESSION_UID}&user_id=user-1"
            ):
                pass

    assert raised.value.code == 1008
    assert raised.value.reason == "speech_to_speech flow is not configured"


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
