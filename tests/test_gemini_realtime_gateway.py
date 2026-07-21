from collections.abc import AsyncIterator

from google.genai import types

from domain.realtime import (
    AudioChunk,
    RealtimeModelAudioDelta,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
)
from domain.tools import ToolResult
from infrastructure.gemini_realtime_gateway import GeminiRealtimeModelSession


class FakeGeminiRealtimeSession:
    """Capture Gemini Live requests and yield a finite deterministic turn."""

    def __init__(self, messages: list[types.LiveServerMessage]) -> None:
        """Initialize message delivery and outbound request captures."""
        self._messages = messages
        self._receive_count = 0
        self.realtime_input: list[dict[str, object]] = []
        self.tool_responses: list[list[types.FunctionResponse]] = []

    async def send_realtime_input(self, **kwargs: object) -> None:
        """Capture audio, stream-end, or text inputs."""
        self.realtime_input.append(kwargs)

    async def send_tool_response(
        self,
        *,
        function_responses: list[types.FunctionResponse],
    ) -> None:
        """Capture translated function responses."""
        self.tool_responses.append(function_responses)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        """Yield configured messages only during the first SDK receive iterator."""
        self._receive_count += 1
        if self._receive_count == 1:
            for message in self._messages:
                yield message


async def test_gemini_realtime_session_translates_full_duplex_media_and_tools() -> None:
    """Normalize both transcript directions, audio, calls, and turn completion."""
    provider = FakeGeminiRealtimeSession(
        [
            types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text="Pregunta"),
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
                    output_transcription=types.Transcription(text="Respuesta"),
                )
            ),
            types.LiveServerMessage(
                tool_call=types.LiveServerToolCall(
                    function_calls=[
                        types.FunctionCall(id="call-1", name="lookup", args={"value": 7})
                    ]
                )
            ),
            types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True)),
        ]
    )
    session = GeminiRealtimeModelSession(provider)  # type: ignore[arg-type]

    await session.send_audio(AudioChunk(data=b"\x00\x00"))
    await session.end_audio()
    await session.send_text("Texto")
    stream = session.receive()
    events = [await anext(stream) for _ in range(5)]
    tool_event = next(event for event in events if isinstance(event, RealtimeModelToolCall))
    await session.send_tool_results(
        (ToolResult(call_id=tool_event.calls[0].call_id, output={"found": True}),)
    )
    await stream.aclose()

    assert provider.realtime_input[0]["audio"] == types.Blob(
        data=b"\x00\x00",
        mime_type="audio/pcm;rate=16000",
    )
    assert provider.realtime_input[1] == {"audio_stream_end": True}
    assert provider.realtime_input[2] == {"text": "Texto"}
    assert isinstance(events[0], RealtimeModelAudioDelta)
    assert events[1] == RealtimeModelInputTranscriptDelta(text="Pregunta")
    assert events[2] == RealtimeModelOutputTranscriptDelta(text="Respuesta")
    assert isinstance(events[3], RealtimeModelToolCall)
    assert isinstance(events[4], RealtimeModelTurnCompleted)
    response = provider.tool_responses[0][0]
    assert response.id == "call-1"
    assert response.name == "lookup"
    assert response.response == {"ok": True, "result": {"found": True}}
