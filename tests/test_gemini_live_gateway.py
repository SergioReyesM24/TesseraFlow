from collections.abc import AsyncIterator

import pytest
from google.genai import types

from domain.conversations import ConversationMessage
from domain.tools import ToolCall, ToolResult, ToolSpec
from domain.turn_events import ModelAudioDelta, ModelStreamCompleted, ModelTextDelta
from infrastructure.gemini_live_gateway import (
    GeminiLiveGateway,
    GeminiLiveModelSession,
    GeminiLiveSessionStateError,
)


def test_gemini_gateway_translates_tools_and_retained_history() -> None:
    """Prefill Gemini without leaking its content format into stored conversations."""
    declaration = GeminiLiveGateway._to_function_declaration(
        ToolSpec(
            name="lookup",
            description="Find one record.",
            arguments_schema={"type": "object", "properties": {}},
        )
    )
    history = GeminiLiveGateway._to_history(
        (
            ConversationMessage(role="user", content="Busca el registro"),
            ToolCall(call_id="call-1", tool_name="lookup", arguments={}),
            ToolResult(call_id="call-1", output={"found": True}),
            ConversationMessage(role="assistant", content="Encontrado"),
        )
    )

    assert declaration.name == "lookup"
    assert declaration.parameters_json_schema == {"type": "object", "properties": {}}
    assert [content.role for content in history] == ["user", "model", "user", "model"]
    assert history[1].parts[0].function_call.id == "call-1"  # type: ignore[union-attr]
    response = history[2].parts[0].function_response
    assert response is not None
    assert response.response == {"ok": True, "result": {"found": True}}


class FakeGeminiSession:
    """Expose deterministic SDK messages and capture outbound live requests."""

    def __init__(self, messages: list[types.LiveServerMessage]) -> None:
        """Initialize provider messages and outbound request logs."""
        self.messages = messages
        self.realtime_input: list[dict[str, object]] = []
        self.tool_responses: list[list[types.FunctionResponse]] = []

    async def send_realtime_input(self, **kwargs: object) -> None:
        """Capture conversational text sent through the realtime channel."""
        self.realtime_input.append(kwargs)

    async def send_tool_response(
        self,
        *,
        function_responses: list[types.FunctionResponse],
    ) -> None:
        """Capture the translated result batch."""
        self.tool_responses.append(function_responses)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        """Yield provider messages while allowing pauses at tool boundaries."""
        for message in self.messages:
            yield message


async def test_gemini_session_streams_audio_transcript_and_common_completion() -> None:
    """Implement the same ModelSession contract used by the text provider."""
    provider = FakeGeminiSession(
        [
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x01\x02",
                                    mime_type="audio/pcm;rate=24000",
                                )
                            )
                        ],
                    ),
                    output_transcription=types.Transcription(text="Hola"),
                    turn_complete=True,
                )
            )
        ]
    )
    session = GeminiLiveModelSession(provider)  # type: ignore[arg-type]

    events = [event async for event in session.stream_message("Saluda")]

    assert provider.realtime_input == [{"text": "Saluda"}]
    assert events[0] == ModelAudioDelta(data=b"\x01\x02", mime_type="audio/pcm;rate=24000")
    assert events[1] == ModelTextDelta(text="Hola")
    assert isinstance(events[2], ModelStreamCompleted)
    assert events[2].reply.text == "Hola"


async def test_gemini_session_pauses_for_tools_and_continues_the_same_turn() -> None:
    """Preserve the SDK receive iterator while AgentService executes a tool."""
    provider = FakeGeminiSession(
        [
            types.LiveServerMessage(
                tool_call=types.LiveServerToolCall(
                    function_calls=[types.FunctionCall(id="call-1", name="lookup", args={"id": 7})]
                )
            ),
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    output_transcription=types.Transcription(text="Encontrado"),
                    turn_complete=True,
                )
            ),
        ]
    )
    session = GeminiLiveModelSession(provider)  # type: ignore[arg-type]

    first = [event async for event in session.stream_message("Busca")]
    boundary = first[-1]
    assert isinstance(boundary, ModelStreamCompleted)
    assert boundary.reply.tool_calls[0].tool_name == "lookup"
    with pytest.raises(GeminiLiveSessionStateError):
        await session.send_tool_results((ToolResult(call_id="wrong", output={}),))
    second = [
        event
        async for event in session.stream_tool_results(
            (ToolResult(call_id="call-1", output={"found": True}),)
        )
    ]

    assert second[0] == ModelTextDelta(text="Encontrado")
    assert isinstance(second[-1], ModelStreamCompleted)
    assert second[-1].reply.text == "Encontrado"
    response = provider.tool_responses[0][0]
    assert response.id == "call-1"
    assert response.name == "lookup"
    assert response.response == {"ok": True, "result": {"found": True}}


async def test_gemini_session_rejects_a_second_initial_message() -> None:
    """Keep one model session isolated to exactly one application execution."""
    provider = FakeGeminiSession([])
    session = GeminiLiveModelSession(provider)  # type: ignore[arg-type]
    first = session.stream_message("Primero")
    await first.aclose()

    with pytest.raises(GeminiLiveSessionStateError):
        session.stream_message("Segundo")
