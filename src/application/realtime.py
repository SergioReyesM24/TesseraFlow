import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import structlog

from application.conversations import ConversationConflictError, ConversationNotFoundError
from application.ports import (
    ConversationRepository,
    InteractionNotifier,
    InteractionRepository,
    RealtimeModelGateway,
    RealtimeModelSession,
)
from application.tools import ToolExecutionContext, ToolExecutor, ToolRegistry
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import ConversationItem, ConversationKey, ConversationMessage
from domain.interactions import InteractionCommand, InteractionSource
from domain.realtime import (
    AudioChunk,
    RealtimeActivityEnded,
    RealtimeActivityStarted,
    RealtimeAgentEvent,
    RealtimeAudioDelta,
    RealtimeAudioInterrupted,
    RealtimeConnectionState,
    RealtimeInputTranscriptDelta,
    RealtimeModelActivityEnded,
    RealtimeModelActivityStarted,
    RealtimeModelAudioDelta,
    RealtimeModelAudioInterrupted,
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
from domain.tools import ToolCallRecord, ToolResult

logger = structlog.get_logger(__name__)


class RealtimeSessionStateError(RuntimeError):
    """Raised when client media controls violate realtime session ordering."""


class RealtimeAudioChunkError(ValueError):
    """Raised when an audio fragment violates the configured PCM boundary."""


class RealtimeToolRoundsExceededError(RuntimeError):
    """Raised when one speech turn exceeds its allowed model tool rounds."""


class RealtimeBackpressureError(RuntimeError):
    """Raised when the bounded provider writer cannot accept work in time."""


class RealtimeUnsupportedOptionError(ValueError):
    """Raised when session options request an adapter capability it lacks."""


OutboundKind = Literal[
    "audio",
    "audio_end",
    "activity_start",
    "activity_end",
    "text",
    "tool_results",
    "a2a_completion",
    "close",
]


@dataclass(slots=True)
class _OutboundCommand:
    """One acknowledged command owned exclusively by the provider writer."""

    sequence: int
    kind: OutboundKind
    payload: AudioChunk | str | tuple[ToolResult, ...] | None
    completion: asyncio.Future[None]
    audio_bytes: int = 0


class RealtimeAgentService:
    """Open full-duplex sessions over a provider-neutral realtime gateway."""

    def __init__(
        self,
        model_gateway: RealtimeModelGateway,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        interactions: InteractionRepository | None,
        notifier: InteractionNotifier | None,
        *,
        max_audio_chunk_bytes: int,
        max_tool_rounds: int,
        max_session_seconds: float,
        outbound_max_messages: int,
        outbound_max_audio_bytes: int,
        outbound_enqueue_timeout_seconds: float,
        proactive_turn_timeout_seconds: float,
        command_reconciliation_seconds: float,
    ) -> None:
        """Bind application ports, authorized tools, and bounded session limits."""
        self._model_gateway = model_gateway
        self._tools = tools
        self._conversations = conversations
        self._interactions = interactions
        self._notifier = notifier
        self._max_audio_chunk_bytes = max_audio_chunk_bytes
        self._max_tool_rounds = max_tool_rounds
        self._max_session_seconds = max_session_seconds
        self._outbound_max_messages = outbound_max_messages
        self._outbound_max_audio_bytes = outbound_max_audio_bytes
        self._outbound_enqueue_timeout_seconds = outbound_enqueue_timeout_seconds
        self._proactive_turn_timeout_seconds = proactive_turn_timeout_seconds
        self._command_reconciliation_seconds = command_reconciliation_seconds

    @property
    def capabilities(self) -> RealtimeSessionCapabilities:
        """Expose adapter features without leaking its concrete implementation."""
        return self._model_gateway.capabilities

    def open_session(
        self,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
        options: RealtimeSessionOptions | None = None,
    ) -> AbstractAsyncContextManager["RealtimeAgentSession"]:
        """Open one provider connection scoped to an authenticated client socket."""
        return self._open_session(
            definition,
            conversation_key,
            options or RealtimeSessionOptions(),
        )

    @asynccontextmanager
    async def _open_session(
        self,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
        options: RealtimeSessionOptions,
    ) -> AsyncIterator["RealtimeAgentSession"]:
        """Load retained history and own all connection-local background tasks."""
        if options.activity.detection not in self.capabilities.activity_detection_modes:
            raise RealtimeUnsupportedOptionError(
                f"Activity mode {options.activity.detection} is not supported"
            )
        if options.activity.interrupt_on_activity and not self.capabilities.supports_barge_in:
            raise RealtimeUnsupportedOptionError("Barge-in is not supported by this adapter")
        conversation = await self._conversations.load(conversation_key)
        if conversation is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        selected_tools = self._tools.select(definition.tool_names)
        async with asyncio.timeout(self._max_session_seconds):
            async with self._model_gateway.open_session(
                definition,
                selected_tools.specs,
                conversation.messages,
                options,
            ) as model_session:
                session = RealtimeAgentSession(
                    model_session,
                    selected_tools,
                    self._conversations,
                    conversation_key,
                    interactions=self._interactions,
                    notifier=self._notifier,
                    activity_detection=options.activity.detection,
                    max_audio_chunk_bytes=self._max_audio_chunk_bytes,
                    max_tool_rounds=self._max_tool_rounds,
                    outbound_max_messages=self._outbound_max_messages,
                    outbound_max_audio_bytes=self._outbound_max_audio_bytes,
                    outbound_enqueue_timeout_seconds=(self._outbound_enqueue_timeout_seconds),
                    proactive_turn_timeout_seconds=self._proactive_turn_timeout_seconds,
                    command_reconciliation_seconds=self._command_reconciliation_seconds,
                )
                async with session.lifecycle():
                    yield session


class RealtimeAgentSession:
    """Serialize provider writes, tools, persistence, and proactive completions."""

    def __init__(
        self,
        model_session: RealtimeModelSession,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        conversation_key: ConversationKey,
        *,
        max_audio_chunk_bytes: int,
        max_tool_rounds: int,
        interactions: InteractionRepository | None = None,
        notifier: InteractionNotifier | None = None,
        activity_detection: Literal["automatic", "explicit"] = "automatic",
        outbound_max_messages: int = 128,
        outbound_max_audio_bytes: int = 131_072,
        outbound_enqueue_timeout_seconds: float = 5.0,
        proactive_turn_timeout_seconds: float = 120.0,
        command_reconciliation_seconds: float = 5.0,
    ) -> None:
        """Initialize isolated state and a bounded outbound command channel."""
        self._model_session = model_session
        self._tools = tools
        self._conversations = conversations
        self._conversation_key = conversation_key
        self._interactions = interactions
        self._notifier = notifier
        self._activity_detection = activity_detection
        self._max_audio_chunk_bytes = max_audio_chunk_bytes
        self._max_tool_rounds = max_tool_rounds
        self._outbound_enqueue_timeout_seconds = outbound_enqueue_timeout_seconds
        self._outbound_max_audio_bytes = outbound_max_audio_bytes
        self._proactive_turn_timeout_seconds = proactive_turn_timeout_seconds
        self._command_reconciliation_seconds = command_reconciliation_seconds
        self._tool_executor = ToolExecutor()
        self._outbound: asyncio.Queue[_OutboundCommand] = asyncio.Queue(
            maxsize=outbound_max_messages
        )
        self._audio_capacity = asyncio.Condition()
        self._direct_write_lock = asyncio.Lock()
        self._pending_audio_bytes = 0
        self._next_outbound_sequence = 0
        self._writer_task: asyncio.Task[None] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._lifecycle_active = False
        self._worker_id = f"realtime:{uuid4()}"
        self._idle = asyncio.Event()
        self._idle.set()
        self._command_done = asyncio.Event()
        self._active_command: InteractionCommand | None = None
        self._turn_id: str | None = None
        self._pending_audio_turn_id: str | None = None
        self._source: InteractionSource = "speech_user"
        self._connection_state: RealtimeConnectionState = "connected"
        self._accepting_audio = False
        self._turn_has_input = False
        self._turn_has_output = False
        self._input_parts: list[str] = []
        self._output_parts: list[str] = []
        self._turn_items: list[ConversationItem] = []
        self._records: list[ToolCallRecord] = []
        self._tool_rounds = 0

    @property
    def connection_state(self) -> RealtimeConnectionState:
        """Expose the neutral lifecycle state without provider recovery details."""
        return self._connection_state

    @asynccontextmanager
    async def lifecycle(self) -> AsyncIterator[None]:
        """Own writer and durable dispatcher tasks for the socket lifetime."""
        self._lifecycle_active = True
        self._ensure_writer()
        if self._interactions is not None and self._notifier is not None:
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_realtime_commands(),
                name=f"realtime-commands-{self._conversation_key.conversation_id}",
            )
        try:
            yield
        finally:
            await self._close_tasks()
            self._lifecycle_active = False

    async def start_audio(self, turn_id: str) -> None:
        """Begin a logical speech turn and allow subsequent binary PCM frames."""
        if self._accepting_audio:
            raise RealtimeSessionStateError("An audio input stream is already active")
        if self._turn_id is None:
            self._begin_turn(turn_id, source="speech_user")
        else:
            self._pending_audio_turn_id = turn_id
        self._accepting_audio = True
        self._refresh_idle()

    async def send_audio(self, data: bytes) -> None:
        """Validate and enqueue one PCM16 fragment with bounded backpressure."""
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
        await self._enqueue("audio", AudioChunk(data=data), audio_bytes=len(data))

    async def end_audio(self) -> None:
        """Pause capture and enqueue the provider-specific stream boundary."""
        if not self._accepting_audio:
            raise RealtimeSessionStateError("No audio input stream is active")
        self._accepting_audio = False
        if self._activity_detection == "automatic":
            await self._enqueue("audio_end", None)
        self._refresh_idle()

    async def start_activity(self) -> None:
        """Enqueue explicit speech activity start when selected for the session."""
        if self._activity_detection != "explicit":
            raise RealtimeSessionStateError("Explicit activity detection is not configured")
        await self._enqueue("activity_start", None)

    async def end_activity(self) -> None:
        """Enqueue explicit speech activity end when selected for the session."""
        if self._activity_detection != "explicit":
            raise RealtimeSessionStateError("Explicit activity detection is not configured")
        await self._enqueue("activity_end", None)

    async def send_text(self, turn_id: str, text: str) -> None:
        """Send a user text fallback through the persistent realtime connection."""
        if self._accepting_audio:
            raise RealtimeSessionStateError("End the active audio stream before sending text")
        if self._turn_id is not None:
            raise RealtimeSessionStateError("A realtime turn is already active")
        self._begin_turn(turn_id, source="text_user")
        self._input_parts.append(text)
        await self._enqueue("text", text)

    async def events(self) -> AsyncIterator[RealtimeAgentEvent]:
        """Normalize model events while monitoring the durable dispatcher task."""
        iterator = self._model_session.receive().__aiter__()
        while True:
            receive_task: asyncio.Task[RealtimeModelEvent] = asyncio.create_task(
                self._next_model_event(iterator)
            )
            waiters: set[asyncio.Task[Any]] = {receive_task}
            if self._dispatcher_task is not None:
                waiters.add(self._dispatcher_task)
            try:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
                raise
            if self._dispatcher_task is not None and self._dispatcher_task in done:
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
                exception = self._dispatcher_task.exception()
                if exception is not None:
                    raise exception
                raise RealtimeSessionStateError("Realtime command dispatcher stopped")
            try:
                event = receive_task.result()
            except StopAsyncIteration:
                if not self._lifecycle_active:
                    await self._close_tasks()
                return
            async for normalized in self._handle_model_event(event):
                yield normalized

    @staticmethod
    async def _next_model_event(
        iterator: AsyncIterator[RealtimeModelEvent],
    ) -> RealtimeModelEvent:
        """Wrap an async iterator awaitable for task-based failure monitoring."""
        return await anext(iterator)

    async def _handle_model_event(self, event: object) -> AsyncIterator[RealtimeAgentEvent]:
        """Translate one provider-neutral event and advance connection-local state."""
        if isinstance(event, RealtimeModelInputTranscriptDelta):
            await self._activate_audio_turn_for_input()
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
            if self._pending_audio_turn_id is not None:
                await self._requeue_active_command()
                self._begin_turn(self._pending_audio_turn_id, source="speech_user")
            yield RealtimeAudioInterrupted(turn_id=self._ensure_turn_id())
        elif isinstance(event, RealtimeModelToolCall):
            async for tool_event in self._handle_tools(self._ensure_turn_id(), event):
                yield tool_event
        elif isinstance(event, RealtimeModelTurnCompleted):
            turn_id = self._ensure_turn_id()
            yield await self._complete_turn(turn_id, event.response_id)
        elif isinstance(event, RealtimeModelActivityStarted):
            yield RealtimeActivityStarted(turn_id=self._ensure_turn_id())
        elif isinstance(event, RealtimeModelActivityEnded):
            yield RealtimeActivityEnded(turn_id=self._ensure_turn_id())
        elif isinstance(event, RealtimeModelReconnectRequested):
            self._connection_state = "recovering"
            yield RealtimeReconnectRequested(deadline_seconds=event.deadline_seconds)
        elif isinstance(event, RealtimeModelReconnected):
            self._connection_state = "connected"
            yield RealtimeReconnected(resumed=event.resumed)

    async def _handle_tools(
        self,
        turn_id: str,
        event: RealtimeModelToolCall,
    ) -> AsyncIterator[RealtimeAgentEvent]:
        """Execute one tool batch and enqueue results through the single writer."""
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
            ToolExecutionContext.from_conversation(
                self._conversation_key,
                delivery_mode="realtime",
            ),
        )
        self._records.extend(records)
        self._turn_items.extend(event.calls)
        self._turn_items.extend(results)
        for record in records:
            yield RealtimeToolCompleted(turn_id=turn_id, record=record)
        await self._enqueue("tool_results", results)

    async def _complete_turn(self, turn_id: str, response_id: str) -> RealtimeTurnCompleted:
        """Persist one real provider turn before confirming proactive delivery."""
        answer = "".join(self._output_parts).strip()
        user_text = "".join(self._input_parts).strip()
        source = self._source
        active_command = self._active_command
        result = AgentResult(
            answer=answer,
            response_id=response_id,
            conversation_id=self._conversation_key.conversation_id,
            tool_calls=tuple(self._records),
        )
        if user_text:
            turn = (
                ConversationMessage(role="user", content=user_text, source=source),
                *self._turn_items,
                ConversationMessage(role="assistant", content=answer, source="assistant"),
            )
            await self._persist_turn(turn)
        if active_command is not None and active_command.request_id == turn_id:
            assert self._interactions is not None
            await self._interactions.complete(active_command.command_id, self._worker_id)
            self._active_command = None
            self._command_done.set()
        self._turn_id = None
        self._reset_turn_buffers()
        self._refresh_idle()
        return RealtimeTurnCompleted(
            turn_id=turn_id,
            result=result,
            source=source,
            job_id=active_command.request_id if active_command is not None else None,
            causation_id=active_command.causation_id if active_command is not None else None,
        )

    async def _persist_turn(self, turn: tuple[ConversationItem, ...]) -> None:
        """Append against the latest version and retry one concurrent write."""
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

    async def _dispatch_realtime_commands(self) -> None:
        """Claim durable completions only at safe full-duplex turn boundaries."""
        assert self._interactions is not None
        assert self._notifier is not None
        lease_seconds = self._proactive_turn_timeout_seconds + 30.0
        async with self._notifier.subscribe_realtime_commands(
            self._conversation_key.conversation_id
        ) as subscription:
            while True:
                await self._idle.wait()
                checkpoint = subscription.checkpoint()
                command = await self._interactions.claim_next_realtime(
                    self._conversation_key,
                    self._worker_id,
                    lease_seconds,
                )
                if command is None:
                    await subscription.wait_for_change(
                        checkpoint,
                        self._command_reconciliation_seconds,
                    )
                    continue
                self._active_command = command
                self._command_done.clear()
                self._begin_turn(command.request_id, source="worker_agent")
                self._input_parts.append(command.message)
                try:
                    await self._enqueue("a2a_completion", command.message)
                    await asyncio.wait_for(
                        self._command_done.wait(),
                        timeout=self._proactive_turn_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    await self._requeue_active_command()
                    raise
                except Exception:
                    await self._requeue_active_command()
                    raise

    async def _enqueue(
        self,
        kind: OutboundKind,
        payload: AudioChunk | str | tuple[ToolResult, ...] | None,
        *,
        audio_bytes: int = 0,
    ) -> None:
        """Wait for bounded capacity and acknowledgement from the sole writer."""
        if not self._lifecycle_active and self._writer_task is None:
            async with self._direct_write_lock:
                await self._write_to_model(kind, payload)
            return
        self._ensure_writer()
        if audio_bytes:
            try:
                async with asyncio.timeout(self._outbound_enqueue_timeout_seconds):
                    async with self._audio_capacity:
                        await self._audio_capacity.wait_for(
                            lambda: (
                                self._pending_audio_bytes + audio_bytes
                                <= self._outbound_max_audio_bytes
                            )
                        )
                        self._pending_audio_bytes += audio_bytes
            except TimeoutError as exc:
                raise RealtimeBackpressureError(
                    "Realtime audio byte capacity was exhausted"
                ) from exc
        self._next_outbound_sequence += 1
        command = _OutboundCommand(
            sequence=self._next_outbound_sequence,
            kind=kind,
            payload=payload,
            completion=asyncio.get_running_loop().create_future(),
            audio_bytes=audio_bytes,
        )
        queued = False
        try:
            await asyncio.wait_for(
                self._outbound.put(command),
                timeout=self._outbound_enqueue_timeout_seconds,
            )
            queued = True
            await command.completion
        except asyncio.CancelledError:
            if not queued:
                await self._release_audio_capacity(audio_bytes)
            raise
        except TimeoutError as exc:
            await self._release_audio_capacity(audio_bytes)
            raise RealtimeBackpressureError("Realtime outbound queue was exhausted") from exc

    def _ensure_writer(self) -> None:
        """Start exactly one connection-local provider writer lazily."""
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(
                self._run_writer(),
                name=f"realtime-writer-{self._conversation_key.conversation_id}",
            )

    async def _run_writer(self) -> None:
        """Perform every provider write sequentially and acknowledge its caller."""
        while True:
            command = await self._outbound.get()
            try:
                await self._write_to_model(command.kind, command.payload)
            except BaseException as exc:
                if not command.completion.done():
                    command.completion.set_exception(exc)
                raise
            else:
                if not command.completion.done():
                    command.completion.set_result(None)
            finally:
                self._outbound.task_done()
                await self._release_audio_capacity(command.audio_bytes)
            if command.kind == "close":
                return

    async def _write_to_model(
        self,
        kind: OutboundKind,
        payload: AudioChunk | str | tuple[ToolResult, ...] | None,
    ) -> None:
        """Dispatch one already-serialized command through the neutral session port."""
        if kind == "audio":
            assert isinstance(payload, AudioChunk)
            await self._model_session.send_audio(payload)
        elif kind == "audio_end":
            await self._model_session.end_audio()
        elif kind == "activity_start":
            await self._model_session.start_activity()
        elif kind == "activity_end":
            await self._model_session.end_activity()
        elif kind in {"text", "a2a_completion"}:
            assert isinstance(payload, str)
            await self._model_session.send_text(payload)
        elif kind == "close":
            return
        else:
            assert isinstance(payload, tuple)
            await self._model_session.send_tool_results(payload)

    async def _release_audio_capacity(self, audio_bytes: int) -> None:
        """Return byte capacity after a queued audio write leaves the buffer."""
        if not audio_bytes:
            return
        async with self._audio_capacity:
            self._pending_audio_bytes = max(0, self._pending_audio_bytes - audio_bytes)
            self._audio_capacity.notify_all()

    async def _close_tasks(self) -> None:
        """Stop connection tasks and release any claimed durable command."""
        self._connection_state = "disconnected"
        await self._requeue_active_command()
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            await asyncio.gather(self._dispatcher_task, return_exceptions=True)
        await self._stop_writer()
        self._dispatcher_task = None
        self._writer_task = None

    async def _stop_writer(self) -> None:
        """Close the serialized command stream, cancelling only if it cannot drain."""
        writer = self._writer_task
        if writer is None:
            return
        if writer.done():
            await asyncio.gather(writer, return_exceptions=True)
            return
        self._next_outbound_sequence += 1
        close_command = _OutboundCommand(
            sequence=self._next_outbound_sequence,
            kind="close",
            payload=None,
            completion=asyncio.get_running_loop().create_future(),
        )
        try:
            self._outbound.put_nowait(close_command)
            await asyncio.wait_for(
                asyncio.shield(close_command.completion),
                timeout=self._outbound_enqueue_timeout_seconds,
            )
        except (asyncio.QueueFull, TimeoutError):
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)

    async def _requeue_active_command(self) -> None:
        """Return an unconfirmed proactive result to its durable inbox."""
        command = self._active_command
        if command is None or self._interactions is None:
            return
        self._active_command = None
        self._command_done.set()
        try:
            await asyncio.shield(self._interactions.requeue(command.command_id, self._worker_id))
        except Exception as exc:
            logger.warning(
                "realtime_command_requeue_failed",
                command_id=command.command_id,
                error_type=type(exc).__name__,
            )

    def _begin_turn(self, turn_id: str, *, source: InteractionSource) -> None:
        """Start a logical turn and discard any response superseded by barge-in."""
        if not turn_id:
            raise RealtimeSessionStateError("turn_id cannot be empty")
        self._turn_id = turn_id
        self._pending_audio_turn_id = None
        self._source = source
        self._reset_turn_buffers()
        self._refresh_idle()

    async def _activate_audio_turn_for_input(self) -> None:
        """Correlate initial or barge-in speech before exposing transcription."""
        if self._pending_audio_turn_id is not None:
            await self._requeue_active_command()
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

    def _refresh_idle(self) -> None:
        """Wake the durable dispatcher only at a safe provider input boundary."""
        if self._turn_id is None and not self._accepting_audio:
            self._idle.set()
        else:
            self._idle.clear()

    def _ensure_turn_id(self) -> str:
        """Create a server turn ID for provider output lacking explicit input."""
        if self._turn_id is None:
            self._begin_turn(str(uuid4()), source="speech_user")
        return self._require_turn_id()

    def _require_turn_id(self) -> str:
        """Reject provider output that is not associated with a logical turn."""
        if self._turn_id is None:
            raise RealtimeSessionStateError("Realtime model emitted output without an active turn")
        return self._turn_id
