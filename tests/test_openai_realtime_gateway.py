import base64
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from domain.agent import AgentDefinition
from domain.realtime import (
    AudioChunk,
    RealtimeActivityConfig,
    RealtimeModelActivityEnded,
    RealtimeModelActivityStarted,
    RealtimeModelAudioDelta,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
    RealtimeSessionOptions,
)
from domain.tools import ToolResult, ToolSpec
from infrastructure.openai_realtime_gateway import (
    OPENAI_REALTIME_MIME_TYPE,
    OpenAIRealtimeModelSession,
)


class FakeOpenAIRealtimeConnection:
    """Capture official SDK event payloads and yield deterministic server events."""

    def __init__(self, events: list[Any] | None = None) -> None:
        """Store one finite provider stream."""
        self.events = events or []
        self.sent: list[dict[str, Any]] = []

    async def send(self, event: dict[str, Any]) -> None:
        """Capture one client event without network access."""
        self.sent.append(event)

    async def _iterate(self) -> AsyncIterator[Any]:
        """Yield the configured OpenAI event objects."""
        for event in self.events:
            yield event

    def __aiter__(self) -> AsyncIterator[Any]:
        """Expose the finite provider stream."""
        return self._iterate()


async def test_openai_realtime_configures_native_audio_and_streams_input_chunks() -> None:
    """Send each 24 kHz browser fragment directly to the provider audio buffer."""
    connection = FakeOpenAIRealtimeConnection()
    session = OpenAIRealtimeModelSession(connection)
    options = RealtimeSessionOptions(
        activity=RealtimeActivityConfig(
            detection="automatic",
            prefix_padding_ms=200,
            silence_duration_ms=400,
            interrupt_on_activity=True,
        )
    )
    tool = ToolSpec(
        name="lookup",
        description="Look up a value",
        arguments_schema={"type": "object", "properties": {}},
    )

    await session.configure(
        AgentDefinition(model="gpt-realtime-2.1", instructions="Help", tool_names=("lookup",)),
        (tool,),
        (),
        options,
        voice_name="marin",
        transcription_model="gpt-4o-mini-transcribe",
        input_language_code="es",
        reasoning_effort=None,
    )
    await session.send_audio(
        AudioChunk(data=b"\x01\x00\x02\x00", mime_type=OPENAI_REALTIME_MIME_TYPE)
    )

    configuration = connection.sent[0]
    assert configuration["type"] == "session.update"
    assert configuration["session"]["audio"]["input"]["format"] == {
        "type": "audio/pcm",
        "rate": 24_000,
    }
    assert configuration["session"]["audio"]["input"]["transcription"] == {
        "model": "gpt-4o-mini-transcribe",
        "language": "es",
    }
    assert configuration["session"]["audio"]["output"]["voice"] == "marin"
    assert connection.sent[1] == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"\x01\x00\x02\x00").decode("ascii"),
    }


async def test_openai_realtime_normalizes_incremental_audio_and_transcripts() -> None:
    """Expose provider audio and both transcript directions before response completion."""
    connection = FakeOpenAIRealtimeConnection(
        [
            SimpleNamespace(type="input_audio_buffer.speech_started"),
            SimpleNamespace(type="input_audio_buffer.speech_stopped"),
            SimpleNamespace(type="input_audio_buffer.committed", item_id="item-1"),
            SimpleNamespace(
                type="conversation.item.input_audio_transcription.delta",
                item_id="item-1",
                delta="Hola",
            ),
            SimpleNamespace(type="response.created"),
            SimpleNamespace(
                type="response.output_audio_transcript.delta",
                response_id="response-1",
                delta="Buenas",
            ),
            SimpleNamespace(
                type="response.output_audio.delta",
                response_id="response-1",
                delta=base64.b64encode(b"\x03\x00").decode("ascii"),
            ),
            SimpleNamespace(
                type="response.done",
                response=SimpleNamespace(id="response-1", status="completed", output=[]),
            ),
            SimpleNamespace(
                type="conversation.item.input_audio_transcription.completed",
                item_id="item-1",
                transcript="Hola mundo",
            ),
        ]
    )
    events = [event async for event in OpenAIRealtimeModelSession(connection).receive()]

    assert isinstance(events[0], RealtimeModelActivityStarted)
    assert isinstance(events[1], RealtimeModelActivityEnded)
    assert events[2] == RealtimeModelInputTranscriptDelta(text="Hola")
    assert events[3] == RealtimeModelOutputTranscriptDelta(text="Buenas")
    assert events[4] == RealtimeModelAudioDelta(
        data=b"\x03\x00",
        mime_type=OPENAI_REALTIME_MIME_TYPE,
    )
    assert events[5] == RealtimeModelInputTranscriptDelta(text=" mundo")
    assert events[6] == RealtimeModelTurnCompleted(response_id="response-1")


async def test_openai_realtime_round_trips_complete_function_calls() -> None:
    """Normalize a completed call and return its output before requesting continuation."""
    call = SimpleNamespace(
        type="function_call",
        call_id="call-1",
        name="lookup",
        arguments='{"value": 7}',
    )
    connection = FakeOpenAIRealtimeConnection(
        [
            SimpleNamespace(
                type="response.output_item.done",
                response_id="response-1",
                item=call,
            ),
            SimpleNamespace(
                type="response.done",
                response=SimpleNamespace(id="response-1", status="completed", output=[call]),
            ),
        ]
    )
    session = OpenAIRealtimeModelSession(connection)
    events = [event async for event in session.receive()]
    tool_event = events[0]
    assert isinstance(tool_event, RealtimeModelToolCall)
    assert tool_event.calls[0].tool_name == "lookup"
    assert tool_event.calls[0].arguments == {"value": 7}

    await session.send_tool_results(
        (ToolResult(call_id="call-1", output={"found": True}),)
    )

    assert connection.sent == [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"ok": true, "result": {"found": true}}',
            },
        },
        {"type": "response.create"},
    ]


async def test_openai_realtime_normalizes_text_audio_and_cache_usage() -> None:
    """Attach billing counters to the neutral realtime turn boundary."""
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=40,
        input_token_details=SimpleNamespace(
            cached_tokens=30,
            audio_tokens=60,
            cached_tokens_details=SimpleNamespace(audio_tokens=10),
        ),
        output_token_details=SimpleNamespace(audio_tokens=20),
    )
    connection = FakeOpenAIRealtimeConnection(
        [
            SimpleNamespace(
                type="response.done",
                response=SimpleNamespace(
                    id="response-usage",
                    status="completed",
                    output=[],
                    usage=usage,
                ),
            )
        ]
    )

    events = [event async for event in OpenAIRealtimeModelSession(connection).receive()]

    completed = events[0]
    assert isinstance(completed, RealtimeModelTurnCompleted)
    assert completed.usage.input_tokens == 100
    assert completed.usage.cached_input_tokens == 30
    assert completed.usage.cached_input_audio_tokens == 10
    assert completed.usage.input_audio_tokens == 60
    assert completed.usage.output_audio_tokens == 20
