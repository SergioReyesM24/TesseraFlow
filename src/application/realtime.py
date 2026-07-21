import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4

from application.conversations import ConversationConflictError, ConversationNotFoundError
from application.ports import ConversationRepository, RealtimeModelGateway, RealtimeModelSession
from application.tools import ToolExecutionContext, ToolExecutor, ToolRegistry
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import ConversationItem, ConversationKey, ConversationMessage
from domain.interactions import InteractionSource
from domain.realtime import (
    AudioChunk,
    RealtimeAgentEvent,
    RealtimeAudioDelta,
    RealtimeAudioInterrupted,
    RealtimeInputTranscriptDelta,
    RealtimeModelAudioDelta,
    RealtimeModelAudioInterrupted,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
    RealtimeOutputTranscriptDelta,
    RealtimeToolCompleted,
    RealtimeToolStarted,
    RealtimeTurnCompleted,
)
from domain.tools import ToolCallRecord


class RealtimeSessionStateError(RuntimeError):
    """Raised when client media controls violate realtime session ordering."""


class RealtimeAudioChunkError(ValueError):
    """Raised when an audio fragment violates the configured PCM boundary."""


class RealtimeToolRoundsExceededError(RuntimeError):
    """Raised when one speech turn exceeds its allowed model tool rounds."""


class RealtimeAgentService:
    """Open full-duplex agent sessions while keeping providers outside the core."""

    def __init__(
        self,
        model_gateway: RealtimeModelGateway,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        *,
        max_audio_chunk_bytes: int,
        max_tool_rounds: int,
        max_session_seconds: float,
    ) -> None:
        """Bind the realtime gateway, authorized tools, persistence, and hard limits."""
        self._model_gateway = model_gateway
        self._tools = tools
        self._conversations = conversations
        self._max_audio_chunk_bytes = max_audio_chunk_bytes
        self._max_tool_rounds = max_tool_rounds
        self._max_session_seconds = max_session_seconds

    def open_session(
        self,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
    ) -> AbstractAsyncContextManager["RealtimeAgentSession"]:
        """Open one provider connection scoped to an authenticated client socket."""
        return self._open_session(definition, conversation_key)

    @asynccontextmanager
    async def _open_session(
        self,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
    ) -> AsyncIterator["RealtimeAgentSession"]:
        """Load history once and release the provider session on socket shutdown."""
        conversation = await self._conversations.load(conversation_key)
        if conversation is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        selected_tools = self._tools.select(definition.tool_names)
        async with asyncio.timeout(self._max_session_seconds):
            async with self._model_gateway.open_session(
                definition,
                selected_tools.specs,
                conversation.messages,
            ) as model_session:
                yield RealtimeAgentSession(
                    model_session,
                    selected_tools,
                    self._conversations,
                    conversation_key,
                    max_audio_chunk_bytes=self._max_audio_chunk_bytes,
                    max_tool_rounds=self._max_tool_rounds,
                )


class RealtimeAgentSession:
    """Orchestrate media, tools, and durable transcripts for one open connection."""

    def __init__(
        self,
        model_session: RealtimeModelSession,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        conversation_key: ConversationKey,
        *,
        max_audio_chunk_bytes: int,
        max_tool_rounds: int,
    ) -> None:
        """Initialize connection-local state without retaining raw user audio."""
        self._model_session = model_session
        self._tools = tools
        self._conversations = conversations
        self._conversation_key = conversation_key
        self._max_audio_chunk_bytes = max_audio_chunk_bytes
        self._max_tool_rounds = max_tool_rounds
        self._tool_executor = ToolExecutor()
        self._turn_id: str | None = None
        self._pending_audio_turn_id: str | None = None
        self._source: InteractionSource = "speech_user"
        self._accepting_audio = False
        self._turn_has_input = False
        self._turn_has_output = False
        self._input_parts: list[str] = []
        self._output_parts: list[str] = []
        self._turn_items: list[ConversationItem] = []
        self._records: list[ToolCallRecord] = []
        self._tool_rounds = 0

    async def start_audio(self, turn_id: str) -> None:
        """Begin a logical speech turn and allow subsequent binary PCM frames."""
        if self._accepting_audio:
            raise RealtimeSessionStateError("An audio input stream is already active")
        if self._turn_id is None:
            self._begin_turn(turn_id, source="speech_user")
        else:
            self._pending_audio_turn_id = turn_id
        self._accepting_audio = True

    async def send_audio(self, data: bytes) -> None:
        """Validate and forward one PCM16 fragment with natural provider backpressure."""
        if not self._accepting_audio:
            raise RealtimeSessionStateError("Send audio_start before binary audio frames")
        if not data:
            raise RealtimeAudioChunkError("Audio chunks cannot be empty")
        if len(data) > self._max_audio_chunk_bytes:
            raise RealtimeAudioChunkError(
                f"Audio chunk exceeds {self._max_audio_chunk_bytes} bytes"
            )
        if len(data) % 2:
            raise RealtimeAudioChunkError("PCM16 audio chunks must contain complete samples")
        await self._model_session.send_audio(AudioChunk(data=data))

    async def end_audio(self) -> None:
        """Pause the active audio stream while Gemini completes its VAD-driven turn."""
        if not self._accepting_audio:
            raise RealtimeSessionStateError("No audio input stream is active")
        self._accepting_audio = False
        await self._model_session.end_audio()

    async def send_text(self, turn_id: str, text: str) -> None:
        """Send a text fallback turn through the persistent realtime connection."""
        if self._accepting_audio:
            raise RealtimeSessionStateError("End the active audio stream before sending text")
        self._begin_turn(turn_id, source="text_user")
        self._input_parts.append(text)
        await self._model_session.send_text(text)

    async def events(self) -> AsyncIterator[RealtimeAgentEvent]:
        """Normalize provider events, execute tools, and persist completed transcripts."""
        async for event in self._model_session.receive():
            if isinstance(event, RealtimeModelInputTranscriptDelta):
                self._activate_audio_turn_for_input()
                turn_id = self._require_turn_id()
                self._input_parts.append(event.text)
                self._turn_has_input = True
                yield RealtimeInputTranscriptDelta(turn_id=turn_id, text=event.text)
            elif isinstance(event, RealtimeModelOutputTranscriptDelta):
                turn_id = self._ensure_turn_id()
                self._output_parts.append(event.text)
                self._turn_has_output = True
                yield RealtimeOutputTranscriptDelta(turn_id=turn_id, text=event.text)
            elif isinstance(event, RealtimeModelAudioDelta):
                turn_id = self._ensure_turn_id()
                self._turn_has_output = True
                yield RealtimeAudioDelta(
                    turn_id=turn_id,
                    data=event.data,
                    mime_type=event.mime_type,
                )
            elif isinstance(event, RealtimeModelAudioInterrupted):
                turn_id = self._ensure_turn_id()
                yield RealtimeAudioInterrupted(turn_id=turn_id)
            elif isinstance(event, RealtimeModelToolCall):
                turn_id = self._ensure_turn_id()
                async for tool_event in self._handle_tools(turn_id, event):
                    yield tool_event
            elif isinstance(event, RealtimeModelTurnCompleted):
                turn_id = self._ensure_turn_id()
                yield await self._complete_turn(turn_id, event.response_id)

    async def _handle_tools(
        self,
        turn_id: str,
        event: RealtimeModelToolCall,
    ) -> AsyncIterator[RealtimeAgentEvent]:
        """Execute one provider-neutral tool batch and resume the live generation."""
        self._tool_rounds += 1
        if self._tool_rounds > self._max_tool_rounds:
            raise RealtimeToolRoundsExceededError(
                f"Realtime turn exceeded {self._max_tool_rounds} tool rounds"
            )
        for call in event.calls:
            yield RealtimeToolStarted(
                turn_id=turn_id,
                call_id=call.call_id,
                tool_name=call.tool_name,
            )
        records, results = await self._tool_executor.execute(
            event.calls,
            self._tools,
            ToolExecutionContext.from_conversation(self._conversation_key),
        )
        self._records.extend(records)
        self._turn_items.extend(event.calls)
        self._turn_items.extend(results)
        for record in records:
            yield RealtimeToolCompleted(turn_id=turn_id, record=record)
        await self._model_session.send_tool_results(results)

    async def _complete_turn(self, turn_id: str, response_id: str) -> RealtimeTurnCompleted:
        """Persist only transcripts and semantic tool exchanges, then reset turn state."""
        answer = "".join(self._output_parts).strip()
        user_text = "".join(self._input_parts).strip()
        result = AgentResult(
            answer=answer,
            response_id=response_id,
            conversation_id=self._conversation_key.conversation_id,
            tool_calls=tuple(self._records),
        )
        if user_text:
            turn = (
                ConversationMessage(role="user", content=user_text, source=self._source),
                *self._turn_items,
                ConversationMessage(role="assistant", content=answer, source="assistant"),
            )
            await self._persist_turn(turn)
        self._turn_id = None
        self._reset_turn_buffers()
        return RealtimeTurnCompleted(turn_id=turn_id, result=result)

    async def _persist_turn(self, turn: tuple[ConversationItem, ...]) -> None:
        """Append against the latest version and retry one concurrent semantic write."""
        for attempt in range(2):
            conversation = await self._conversations.load(self._conversation_key)
            if conversation is None:
                raise ConversationNotFoundError("Conversation session does not exist")
            try:
                await self._conversations.save_turn(conversation, turn)
            except ConversationConflictError:
                if attempt == 1:
                    raise
            else:
                return
        raise AssertionError("Unreachable")

    def _begin_turn(self, turn_id: str, *, source: InteractionSource) -> None:
        """Start a new logical turn, discarding any response superseded by barge-in."""
        if not turn_id:
            raise RealtimeSessionStateError("turn_id cannot be empty")
        self._turn_id = turn_id
        self._pending_audio_turn_id = None
        self._source = source
        self._reset_turn_buffers()

    def _activate_audio_turn_for_input(self) -> None:
        """Correlate initial or barge-in speech before exposing its transcription."""
        if self._pending_audio_turn_id is not None:
            self._begin_turn(self._pending_audio_turn_id, source="speech_user")
        elif self._turn_id is None or self._turn_has_output:
            self._begin_turn(str(uuid4()), source="speech_user")

    def _reset_turn_buffers(self) -> None:
        """Discard connection-local transcript and audit buffers between turns."""
        self._input_parts = []
        self._output_parts = []
        self._turn_items = []
        self._records = []
        self._tool_rounds = 0
        self._turn_has_input = False
        self._turn_has_output = False

    def _ensure_turn_id(self) -> str:
        """Create a server turn ID for VAD-driven output lacking an explicit start."""
        if self._turn_id is None:
            self._begin_turn(str(uuid4()), source="speech_user")
        return self._require_turn_id()

    def _require_turn_id(self) -> str:
        """Reject provider output that is not associated with client input."""
        if self._turn_id is None:
            raise RealtimeSessionStateError("Realtime model emitted output without an active turn")
        return self._turn_id
