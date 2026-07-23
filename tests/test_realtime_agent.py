import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from application.ports import RealtimeModelSession
from application.realtime import (
    RealtimeAgentService,
    RealtimeAgentSession,
    RealtimeAudioChunkError,
    RealtimeBackpressureError,
    RealtimeSessionStateError,
    RealtimeUnsupportedOptionError,
)
from application.tools import AgentTool, ToolArguments, ToolExecutionContext, ToolRegistry
from domain.agent import AgentDefinition
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.interactions import InteractionCommand
from domain.realtime import (
    AudioChunk,
    RealtimeActivityConfig,
    RealtimeActivityEnded,
    RealtimeActivityStarted,
    RealtimeAudioDelta,
    RealtimeInputTranscriptDelta,
    RealtimeModelActivityEnded,
    RealtimeModelActivityStarted,
    RealtimeModelAudioDelta,
    RealtimeModelEvent,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelReconnected,
    RealtimeModelReconnectRequested,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
    RealtimeOutputTranscriptDelta,
    RealtimeReconnected,
    RealtimeReconnectRequested,
    RealtimeSessionCapabilities,
    RealtimeSessionOptions,
    RealtimeToolCompleted,
    RealtimeToolStarted,
    RealtimeTurnCompleted,
)
from domain.tools import ToolCall, ToolResult, ToolSpec


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
        return {
            "value": arguments.value,
            "user_id": context.user_id,
            "delivery_mode": context.delivery_mode,
        }


class StubRealtimeModelSession(RealtimeModelSession):
    """Capture client media and expose deterministic provider events."""

    def __init__(self, events: list[RealtimeModelEvent]) -> None:
        """Initialize outbound captures and the provider event sequence."""
        self._events = events
        self.audio: list[AudioChunk] = []
        self.audio_ended = False
        self.text: list[str] = []
        self.tool_results: list[tuple[ToolResult, ...]] = []
        self.activities: list[str] = []

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Capture one validated PCM fragment."""
        self.audio.append(chunk)

    async def end_audio(self) -> None:
        """Capture the audio stream boundary."""
        self.audio_ended = True

    async def start_activity(self) -> None:
        """Capture one explicit activity boundary."""
        self.activities.append("start")

    async def end_activity(self) -> None:
        """Capture one explicit activity boundary."""
        self.activities.append("end")

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
        *,
        turn_id: str,
    ) -> Conversation:
        """Append one semantic turn without retaining any raw audio."""
        del turn_id
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


class QueueRealtimeModelSession(StubRealtimeModelSession):
    """Keep one provider stream open so durable commands can arrive asynchronously."""

    def __init__(self) -> None:
        """Initialize an empty event channel and observable text-write barrier."""
        super().__init__([])
        self.events_queue: asyncio.Queue[RealtimeModelEvent | None] = asyncio.Queue()
        self.text_sent = asyncio.Event()
        self.receive_calls = 0
        self.subsequent_receive_started = asyncio.Event()

    async def send_text(self, text: str) -> None:
        """Capture proactive input and wake the test producer."""
        await super().send_text(text)
        self.text_sent.set()

    async def receive(self) -> AsyncIterator[RealtimeModelEvent]:
        """Yield provider events until the test closes the stream."""
        self.receive_calls += 1
        if self.receive_calls > 1:
            self.subsequent_receive_started.set()
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            yield event


class StubInteractionSubscription:
    """Provide reconciliation waits without carrying durable command data."""

    def checkpoint(self) -> int:
        """Return one stable generation for polling-based test delivery."""
        return 0

    async def wait_for_change(self, checkpoint: int, deadline_seconds: float) -> None:
        """Yield control briefly so a missing command does not spin."""
        del checkpoint
        await asyncio.sleep(min(deadline_seconds, 0.01))


class StubInteractionNotifier:
    """Open conversation-scoped subscriptions for realtime dispatcher tests."""

    def __init__(self) -> None:
        """Track every conversation observed by a live session."""
        self.conversations: list[str] = []

    @asynccontextmanager
    async def subscribe_realtime_commands(
        self,
        conversation_id: str,
    ) -> AsyncIterator[StubInteractionSubscription]:
        """Yield one reconciliation subscription for the requested conversation."""
        self.conversations.append(conversation_id)
        yield StubInteractionSubscription()


class StubRealtimeInteractions:
    """Lease one realtime completion and record its terminal transition."""

    def __init__(self, command: InteractionCommand) -> None:
        """Initialize one queued durable command."""
        self.command = command
        self.claimed_by: str | None = None
        self.completed: list[tuple[str, str]] = []
        self.requeued: list[tuple[str, str]] = []

    async def claim_next_realtime(
        self,
        conversation: ConversationKey,
        worker_id: str,
        lease_seconds: float,
    ) -> InteractionCommand | None:
        """Lease the command only through its exact conversation ownership key."""
        assert conversation == self.command.conversation
        assert lease_seconds > 0
        if self.claimed_by is not None or self.command.status != "queued":
            return None
        self.claimed_by = worker_id
        self.command = replace(self.command, status="running", attempt_count=1)
        return self.command

    async def complete(self, command_id: str, worker_id: str) -> None:
        """Record completion only for the current lease owner."""
        assert command_id == self.command.command_id
        assert worker_id == self.claimed_by
        self.command = replace(self.command, status="completed")
        self.completed.append((command_id, worker_id))

    async def requeue(self, command_id: str, worker_id: str) -> None:
        """Return an interrupted claim to the durable queue."""
        assert command_id == self.command.command_id
        assert worker_id == self.claimed_by
        self.claimed_by = None
        self.command = replace(self.command, status="queued")
        self.requeued.append((command_id, worker_id))


class SerialWriteRealtimeModelSession(StubRealtimeModelSession):
    """Detect overlapping provider writes and optionally block the first call."""

    def __init__(self, *, block_writes: bool = False) -> None:
        """Initialize concurrency counters and a controllable write gate."""
        super().__init__([])
        self.active_writes = 0
        self.max_active_writes = 0
        self.first_write_started = asyncio.Event()
        self.release_writes = asyncio.Event()
        if not block_writes:
            self.release_writes.set()

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Hold one write while recording whether another enters concurrently."""
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        self.first_write_started.set()
        try:
            await self.release_writes.wait()
            await asyncio.sleep(0)
            await super().send_audio(chunk)
        finally:
            self.active_writes -= 1


class AlternateRealtimeGateway:
    """Demonstrate that application code accepts a non-Gemini STS adapter."""

    def __init__(self, model: StubRealtimeModelSession) -> None:
        """Bind one neutral model session and capture handshake options."""
        self.model = model
        self.options: list[RealtimeSessionOptions] = []

    @property
    def capabilities(self) -> RealtimeSessionCapabilities:
        """Advertise a deliberately different media and activity profile."""
        return RealtimeSessionCapabilities(
            input_audio_mime_type="audio/pcm;rate=8000",
            output_audio_mime_type="audio/pcm;rate=16000",
            activity_detection_modes=("explicit",),
            supports_barge_in=False,
            recovery_mode="restart",
        )

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
        options: RealtimeSessionOptions,
    ) -> AsyncIterator[StubRealtimeModelSession]:
        """Open through neutral arguments without provider-specific branching."""
        del definition, tools, history
        self.options.append(options)
        yield self.model


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
        (
            ToolResult(
                call_id="call-1",
                output={
                    "value": 7,
                    "user_id": "user-1",
                    "delivery_mode": "realtime",
                },
            ),
        )
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


async def test_realtime_explicit_activity_uses_neutral_boundaries() -> None:
    """Forward client-delimited activity without applying automatic stream end."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    model = StubRealtimeModelSession([])
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        StubConversations(key),
        key,
        activity_detection="explicit",
        max_audio_chunk_bytes=16,
        max_tool_rounds=1,
    )

    await session.start_audio("turn-1")
    await session.start_activity()
    await session.end_activity()
    await session.end_audio()

    assert model.activities == ["start", "end"]
    assert model.audio_ended is False


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


async def test_realtime_dispatcher_injects_and_confirms_worker_completion_on_terminal() -> None:
    """Use the active STS session, never the turn-based agent, for realtime jobs."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    command = InteractionCommand(
        command_id="a2a-result:job-1",
        request_id="job-1",
        conversation=key,
        kind="worker_completed",
        source="worker_agent",
        message='{"protocol":"tesseraflow.a2a.result","job_id":"job-1"}',
        delivery_mode="realtime",
        causation_id="job-1",
    )
    interactions = StubRealtimeInteractions(command)
    notifier = StubInteractionNotifier()
    model = QueueRealtimeModelSession()
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        conversations,
        key,
        interactions=interactions,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        command_reconciliation_seconds=0.01,
    )

    async with session.lifecycle():
        stream = session.events()
        first_event = asyncio.create_task(anext(stream))
        async with asyncio.timeout(0.5):
            await model.text_sent.wait()
        assert interactions.completed == []
        await model.events_queue.put(
            RealtimeModelOutputTranscriptDelta(text="El trabajo ha terminado")
        )
        await model.events_queue.put(RealtimeModelTurnCompleted(response_id="realtime-1"))
        output = await first_event
        terminal = await anext(stream)
        await stream.aclose()

    assert isinstance(output, RealtimeOutputTranscriptDelta)
    assert terminal == RealtimeTurnCompleted(
        turn_id="job-1",
        result=terminal.result,
        source="worker_agent",
        job_id="job-1",
        causation_id="job-1",
    )
    assert terminal.result.answer == "El trabajo ha terminado"
    assert len(interactions.completed) == 1
    assert notifier.conversations == ["conversation-1"]
    assert model.text == [command.message]
    assert conversations.saved_turns == [
        (
            ConversationMessage(
                role="user",
                content=command.message,
                source="worker_agent",
            ),
            ConversationMessage(
                role="assistant",
                content="El trabajo ha terminado",
                source="assistant",
            ),
        )
    ]


async def test_realtime_disconnect_drains_visible_proactive_turn_before_releasing_claim() -> None:
    """Persist visible proactive output when the client leaves before its terminal event."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    command = InteractionCommand(
        command_id="a2a-result:job-1",
        request_id="job-1",
        conversation=key,
        kind="worker_completed",
        source="worker_agent",
        message='{"protocol":"tesseraflow.a2a.result","job_id":"job-1"}',
        delivery_mode="realtime",
        causation_id="job-1",
    )
    interactions = StubRealtimeInteractions(command)
    model = QueueRealtimeModelSession()
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        conversations,
        key,
        interactions=interactions,  # type: ignore[arg-type]
        notifier=StubInteractionNotifier(),  # type: ignore[arg-type]
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        command_reconciliation_seconds=0.01,
    )

    async with session.lifecycle():
        stream = session.events()
        visible_output = asyncio.create_task(anext(stream))
        async with asyncio.timeout(0.5):
            await model.text_sent.wait()
        await model.events_queue.put(
            RealtimeModelOutputTranscriptDelta(text="El trabajo ha terminado")
        )
        assert isinstance(await visible_output, RealtimeOutputTranscriptDelta)

        # The provider terminal is already pending when the client disconnects and
        # cancels its event consumer.
        await model.events_queue.put(RealtimeModelTurnCompleted(response_id="realtime-1"))
        await stream.aclose()

    assert interactions.requeued == []
    assert interactions.completed == [(command.command_id, interactions.claimed_by)]
    assert conversations.saved_turns == [
        (
            ConversationMessage(
                role="user",
                content=command.message,
                source="worker_agent",
            ),
            ConversationMessage(
                role="assistant",
                content="El trabajo ha terminado",
                source="assistant",
            ),
        )
    ]


async def test_realtime_disconnect_requeues_visible_turn_without_provider_terminal() -> None:
    """Bound shutdown and avoid persisting an assistant response that never completed."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    command = InteractionCommand(
        command_id="a2a-result:job-1",
        request_id="job-1",
        conversation=key,
        kind="worker_completed",
        source="worker_agent",
        message='{"protocol":"tesseraflow.a2a.result","job_id":"job-1"}',
        delivery_mode="realtime",
        causation_id="job-1",
    )
    interactions = StubRealtimeInteractions(command)
    model = QueueRealtimeModelSession()
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        conversations,
        key,
        interactions=interactions,  # type: ignore[arg-type]
        notifier=StubInteractionNotifier(),  # type: ignore[arg-type]
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        proactive_turn_timeout_seconds=0.01,
        command_reconciliation_seconds=0.01,
    )

    async with asyncio.timeout(0.5):
        async with session.lifecycle():
            stream = session.events()
            visible_output = asyncio.create_task(anext(stream))
            await model.text_sent.wait()
            await model.events_queue.put(RealtimeModelOutputTranscriptDelta(text="Parcial"))
            assert isinstance(await visible_output, RealtimeOutputTranscriptDelta)
            await stream.aclose()

    assert interactions.completed == []
    assert len(interactions.requeued) == 1
    assert conversations.saved_turns == []


async def test_realtime_drain_propagates_cancellation_after_releasing_claim() -> None:
    """Release durable state during cancelled shutdown without hiding cancellation."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    command = InteractionCommand(
        command_id="a2a-result:job-1",
        request_id="job-1",
        conversation=key,
        kind="worker_completed",
        source="worker_agent",
        message='{"protocol":"tesseraflow.a2a.result","job_id":"job-1"}',
        delivery_mode="realtime",
        causation_id="job-1",
    )
    interactions = StubRealtimeInteractions(command)
    model = QueueRealtimeModelSession()
    conversations = StubConversations(key)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        conversations,
        key,
        interactions=interactions,  # type: ignore[arg-type]
        notifier=StubInteractionNotifier(),  # type: ignore[arg-type]
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        command_reconciliation_seconds=0.01,
    )

    async def disconnect_during_drain() -> None:
        """Expose one delta and then enter lifecycle shutdown without a terminal."""
        async with session.lifecycle():
            stream = session.events()
            visible_output = asyncio.create_task(anext(stream))
            await model.text_sent.wait()
            await model.events_queue.put(RealtimeModelOutputTranscriptDelta(text="Parcial"))
            assert isinstance(await visible_output, RealtimeOutputTranscriptDelta)
            await stream.aclose()

    closing = asyncio.create_task(disconnect_during_drain())
    async with asyncio.timeout(0.5):
        await model.subsequent_receive_started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert interactions.completed == []
    assert len(interactions.requeued) == 1
    assert conversations.saved_turns == []


async def test_realtime_single_writer_serializes_concurrent_audio_commands() -> None:
    """Prevent WebSocket producers from invoking provider writes concurrently."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    model = SerialWriteRealtimeModelSession()
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        StubConversations(key),
        key,
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
    )

    async with session.lifecycle():
        await session.start_audio("turn-1")
        await asyncio.gather(*(session.send_audio(b"\x00\x00") for _ in range(8)))

    assert model.max_active_writes == 1
    assert len(model.audio) == 8


async def test_realtime_queue_applies_backpressure_without_dropping_audio() -> None:
    """Fail a saturated producer after its configured bounded wait."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    model = SerialWriteRealtimeModelSession(block_writes=True)
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        StubConversations(key),
        key,
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        outbound_max_messages=1,
        outbound_max_audio_bytes=16,
        outbound_enqueue_timeout_seconds=0.02,
    )

    async with session.lifecycle():
        await session.start_audio("turn-1")
        first = asyncio.create_task(session.send_audio(b"\x00\x00"))
        await model.first_write_started.wait()
        second = asyncio.create_task(session.send_audio(b"\x01\x00"))
        await asyncio.sleep(0)
        with pytest.raises(RealtimeBackpressureError, match="queue was exhausted"):
            await session.send_audio(b"\x02\x00")
        model.release_writes.set()
        await asyncio.gather(first, second)

    assert model.audio == [
        AudioChunk(data=b"\x00\x00"),
        AudioChunk(data=b"\x01\x00"),
    ]


async def test_realtime_normalizes_activity_and_recovery_events() -> None:
    """Expose VAD and recovery state without any Gemini payloads in the core."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    model = StubRealtimeModelSession(
        [
            RealtimeModelActivityStarted(),
            RealtimeModelActivityEnded(),
            RealtimeModelReconnectRequested(deadline_seconds=3.5),
            RealtimeModelReconnected(resumed=True),
        ]
    )
    session = RealtimeAgentSession(
        model,
        ToolRegistry([]),
        StubConversations(key),
        key,
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
    )
    await session.start_audio("turn-1")

    events = [event async for event in session.events()]

    assert session.connection_state == "disconnected"
    assert events == [
        RealtimeActivityStarted(turn_id="turn-1"),
        RealtimeActivityEnded(turn_id="turn-1"),
        RealtimeReconnectRequested(deadline_seconds=3.5),
        RealtimeReconnected(resumed=True),
    ]


async def test_realtime_service_accepts_an_alternate_provider_and_rejects_capabilities() -> None:
    """Select STS behavior from neutral capabilities rather than provider identity."""
    key = ConversationKey(conversation_id="conversation-1", user_id="user-1")
    gateway = AlternateRealtimeGateway(StubRealtimeModelSession([]))
    service = RealtimeAgentService(
        gateway,
        ToolRegistry([]),
        StubConversations(key),
        interactions=None,
        notifier=None,
        max_audio_chunk_bytes=16,
        max_tool_rounds=2,
        max_session_seconds=30,
        outbound_max_messages=4,
        outbound_max_audio_bytes=16,
        outbound_enqueue_timeout_seconds=0.1,
        proactive_turn_timeout_seconds=1,
        command_reconciliation_seconds=0.1,
    )
    definition = AgentDefinition(model="alternate-sts", instructions="Speak", tool_names=())
    supported = RealtimeSessionOptions(
        activity=RealtimeActivityConfig(
            detection="explicit",
            interrupt_on_activity=False,
        )
    )

    async with service.open_session(definition, key, supported):
        pass

    assert gateway.options == [supported]
    assert service.capabilities.recovery_mode == "restart"
    with pytest.raises(RealtimeUnsupportedOptionError, match="Barge-in"):
        async with service.open_session(
            definition,
            key,
            RealtimeSessionOptions(
                activity=RealtimeActivityConfig(
                    detection="explicit",
                    interrupt_on_activity=True,
                )
            ),
        ):
            pass
