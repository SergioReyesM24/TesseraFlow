from collections.abc import AsyncIterator
from typing import Any

import pytest

from application.ports import RealtimeModelSession
from application.realtime import (
    RealtimeAgentSession,
    RealtimeAudioChunkError,
    RealtimeSessionStateError,
)
from application.tools import AgentTool, ToolArguments, ToolExecutionContext, ToolRegistry
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.realtime import (
    AudioChunk,
    RealtimeAudioDelta,
    RealtimeInputTranscriptDelta,
    RealtimeModelAudioDelta,
    RealtimeModelEvent,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
    RealtimeOutputTranscriptDelta,
    RealtimeToolCompleted,
    RealtimeToolStarted,
    RealtimeTurnCompleted,
)
from domain.tools import ToolCall, ToolResult


class LookupArguments(ToolArguments):
    """Arguments accepted by the deterministic realtime test tool."""

    value: int


class LookupTool(AgentTool[LookupArguments]):
    """Return one deterministic object after validating its argument."""

    name = "lookup"
    description = "Look up one value."
    arguments_model = LookupArguments

    async def execute(
        self,
        arguments: LookupArguments,
        context: ToolExecutionContext,
    ) -> Any:
        """Echo the value and owning user without external I/O."""
        return {"value": arguments.value, "user_id": context.user_id}


class StubRealtimeModelSession(RealtimeModelSession):
    """Capture client media and expose deterministic provider events."""

    def __init__(self, events: list[RealtimeModelEvent]) -> None:
        """Initialize outbound captures and the provider event sequence."""
        self._events = events
        self.audio: list[AudioChunk] = []
        self.audio_ended = False
        self.text: list[str] = []
        self.tool_results: list[tuple[ToolResult, ...]] = []

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Capture one validated PCM fragment."""
        self.audio.append(chunk)

    async def end_audio(self) -> None:
        """Capture the audio stream boundary."""
        self.audio_ended = True

    async def send_text(self, text: str) -> None:
        """Capture a text fallback turn."""
        self.text.append(text)

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Capture one complete result batch."""
        self.tool_results.append(results)

    async def receive(self) -> AsyncIterator[RealtimeModelEvent]:
        """Yield the configured model events in order."""
        for event in self._events:
            yield event


class StubConversations:
    """Persist completed realtime turns in memory."""

    def __init__(self, key: ConversationKey) -> None:
        """Initialize one existing empty conversation."""
        self.conversation = Conversation(key=key)
        self.saved_turns: list[tuple[ConversationItem, ...]] = []

    async def create(self, key: ConversationKey) -> Conversation:
        """Replace the in-memory aggregate with one empty conversation."""
        self.conversation = Conversation(key=key)
        return self.conversation

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Return the aggregate only to its owner."""
        return self.conversation if key == self.conversation.key else None

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Append one semantic turn without retaining any raw audio."""
        assert conversation == self.conversation
        self.saved_turns.append(turn)
        self.conversation = Conversation(
            key=conversation.key,
            messages=conversation.messages + turn,
            version=conversation.version + 1,
        )
        return self.conversation

    async def delete(self, key: ConversationKey) -> bool:
        """Provide the unused repository deletion operation."""
        return key == self.conversation.key


async def test_realtime_session_streams_media_executes_tools_and_persists_transcripts() -> None:
    """Keep media ephemeral while persisting one complete neutral speech turn."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    call = ToolCall(call_id="call-1", tool_name="lookup", arguments={"value": 7})
    model = StubRealtimeModelSession(
        [
            RealtimeModelInputTranscriptDelta(text="Hola"),
            RealtimeModelToolCall(calls=(call,)),
            RealtimeModelAudioDelta(
                data=b"\x01\x02",
                mime_type="audio/pcm;rate=24000",
            ),
            RealtimeModelOutputTranscriptDelta(text="Resultado siete"),
            RealtimeModelTurnCompleted(response_id="response-1"),
        ]
    )
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([LookupTool()]),
        conversations,
        key,
        max_audio_chunk_bytes=16,
        max_tool_rounds=4,
    )

    await session.start_audio("turn-1")
    await session.send_audio(b"\x00\x00\x01\x00")
    await session.end_audio()
    events = [event async for event in session.events()]

    assert model.audio == [AudioChunk(data=b"\x00\x00\x01\x00")]
    assert model.audio_ended is True
    assert isinstance(events[0], RealtimeInputTranscriptDelta)
    assert isinstance(events[1], RealtimeToolStarted)
    assert isinstance(events[2], RealtimeToolCompleted)
    assert events[3] == RealtimeAudioDelta(
        turn_id="turn-1",
        data=b"\x01\x02",
        mime_type="audio/pcm;rate=24000",
    )
    assert events[4] == RealtimeOutputTranscriptDelta(
        turn_id="turn-1",
        text="Resultado siete",
    )
    assert isinstance(events[5], RealtimeTurnCompleted)
    assert events[5].result.answer == "Resultado siete"
    assert model.tool_results == [
        (ToolResult(call_id="call-1", output={"value": 7, "user_id": "user-1"}),)
    ]
    turn = conversations.saved_turns[0]
    assert turn[0] == ConversationMessage(
        role="user",
        content="Hola",
        source="speech_user",
    )
    assert turn[1] == call
    assert isinstance(turn[2], ToolResult)
    assert turn[3] == ConversationMessage(
        role="assistant",
        content="Resultado siete",
        source="assistant",
    )
    assert all(not isinstance(item, AudioChunk) for item in turn)


async def test_realtime_session_rejects_unbounded_or_misaligned_pcm() -> None:
    """Enforce stream state, byte limits, and complete PCM16 samples."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    session = RealtimeAgentSession(
        StubRealtimeModelSession([]),
        ToolRegistry([]),
        StubConversations(key),
        key,
        max_audio_chunk_bytes=4,
        max_tool_rounds=1,
    )

    with pytest.raises(RealtimeSessionStateError):
        await session.send_audio(b"\x00\x00")
    await session.start_audio("turn-1")
    with pytest.raises(RealtimeAudioChunkError, match="complete samples"):
        await session.send_audio(b"\x00")
    with pytest.raises(RealtimeAudioChunkError, match="exceeds"):
        await session.send_audio(b"\x00\x00\x00\x00\x00\x00")
    await session.end_audio()
    with pytest.raises(RealtimeSessionStateError):
        await session.end_audio()


async def test_realtime_session_keeps_continuous_audio_open_across_vad_turns() -> None:
    """Allow Gemini VAD to complete multiple turns without restarting the microphone."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    model = StubRealtimeModelSession(
        [
            RealtimeModelInputTranscriptDelta(text="Primero"),
            RealtimeModelOutputTranscriptDelta(text="Uno"),
            RealtimeModelTurnCompleted(response_id="response-1"),
            RealtimeModelInputTranscriptDelta(text="Segundo"),
            RealtimeModelOutputTranscriptDelta(text="Dos"),
            RealtimeModelTurnCompleted(response_id="response-2"),
        ]
    )
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        conversations,
        key,
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
    )

    await session.start_audio("turn-1")
    events = [event async for event in session.events()]
    completed = [event for event in events if isinstance(event, RealtimeTurnCompleted)]
    await session.send_audio(b"\x00\x00")

    assert len(completed) == 2
    assert completed[0].turn_id == "turn-1"
    assert completed[1].turn_id != completed[0].turn_id
    assert [turn[0].content for turn in conversations.saved_turns] == ["Primero", "Segundo"]  # type: ignore[union-attr]
