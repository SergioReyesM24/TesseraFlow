import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

import structlog

from application.conversations import ConversationConflictError, ConversationNotFoundError
from application.evaluations import (
    ToolCallEvaluationGate,
    ToolCallGateDecision,
    append_evaluation_trace,
    tool_call_revision_limit_results,
)
from application.ports import (
    ConversationRepository,
    EvaluationTraceRepository,
    InteractionNotifier,
    InteractionRepository,
    RealtimeModelGateway,
    RealtimeModelSession,
)
from application.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    extend_visual_components,
)
from domain.a2a import visual_components_from_completion_message
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import ConversationItem, ConversationKey, ConversationMessage
from domain.costs import ModelCallMetrics, ModelCostCalculator, ModelUsage, TurnMetrics
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
    RealtimeVisualComponent,
)
from domain.tools import ToolCall, ToolCallRecord, ToolResult
from domain.visuals import VisualPresentation

logger = structlog.get_logger(__name__)

MAX_DEFERRED_REALTIME_AUDIO_BYTES = 8 * 1024 * 1024


class RealtimeSessionStateError(RuntimeError):
    """Raised when client media controls violate realtime session ordering."""


class RealtimeAudioChunkError(ValueError):
    """Raised when an audio fragment violates the configured PCM boundary."""


class RealtimeToolRoundsExceededError(RuntimeError):
    """Raised when one speech turn exceeds its allowed model tool rounds."""


class RealtimeToolCallEvaluationRejectedError(RuntimeError):
    """Raised when the realtime agent cannot repair a rejected tool-call batch."""


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
        tool_call_gate: ToolCallEvaluationGate | None = None,
        max_tool_call_revisions: int = 2,
        evaluation_traces: EvaluationTraceRepository | None = None,
        cost_calculator: ModelCostCalculator | None = None,
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
        self._tool_call_gate = tool_call_gate
        if max_tool_call_revisions < 0:
            raise ValueError("max_tool_call_revisions cannot be negative")
        self._max_tool_call_revisions = max_tool_call_revisions
        self._evaluation_traces = evaluation_traces
        self._cost_calculator = cost_calculator or ModelCostCalculator()

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
    ) -> AsyncGenerator["RealtimeAgentSession", None]:
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
                    model=definition.model,
                    cost_calculator=self._cost_calculator,
                    interactions=self._interactions,
                    notifier=self._notifier,
                    activity_detection=options.activity.detection,
                    input_audio_mime_type=self.capabilities.input_audio_mime_type,
                    max_audio_chunk_bytes=self._max_audio_chunk_bytes,
                    max_tool_rounds=self._max_tool_rounds,
                    outbound_max_messages=self._outbound_max_messages,
                    outbound_max_audio_bytes=self._outbound_max_audio_bytes,
                    outbound_enqueue_timeout_seconds=(self._outbound_enqueue_timeout_seconds),
                    proactive_turn_timeout_seconds=self._proactive_turn_timeout_seconds,
                    command_reconciliation_seconds=self._command_reconciliation_seconds,
                    tool_call_gate=self._tool_call_gate,
                    max_tool_call_revisions=self._max_tool_call_revisions,
                    evaluation_traces=self._evaluation_traces,
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
        model: str = "unknown",
        cost_calculator: ModelCostCalculator | None = None,
        max_audio_chunk_bytes: int,
        max_tool_rounds: int,
        interactions: InteractionRepository | None = None,
        notifier: InteractionNotifier | None = None,
        activity_detection: Literal["automatic", "explicit"] = "automatic",
        input_audio_mime_type: str = "audio/pcm;rate=16000",
        outbound_max_messages: int = 128,
        outbound_max_audio_bytes: int = 131_072,
        outbound_enqueue_timeout_seconds: float = 5.0,
        proactive_turn_timeout_seconds: float = 120.0,
        command_reconciliation_seconds: float = 5.0,
        tool_call_gate: ToolCallEvaluationGate | None = None,
        max_tool_call_revisions: int = 2,
        evaluation_traces: EvaluationTraceRepository | None = None,
    ) -> None:
        """Initialize isolated state and a bounded outbound command channel."""
        self._model_session = model_session
        self._tools = tools
        self._conversations = conversations
        self._conversation_key = conversation_key
        self._model = model
        self._cost_calculator = cost_calculator or ModelCostCalculator()
        self._interactions = interactions
        self._notifier = notifier
        self._activity_detection = activity_detection
        self._input_audio_mime_type = input_audio_mime_type
        self._max_audio_chunk_bytes = max_audio_chunk_bytes
        self._max_tool_rounds = max_tool_rounds
        self._outbound_enqueue_timeout_seconds = outbound_enqueue_timeout_seconds
        self._outbound_max_audio_bytes = outbound_max_audio_bytes
        self._proactive_turn_timeout_seconds = proactive_turn_timeout_seconds
        self._command_reconciliation_seconds = command_reconciliation_seconds
        self._tool_call_gate = tool_call_gate
        if max_tool_call_revisions < 0:
            raise ValueError("max_tool_call_revisions cannot be negative")
        self._max_tool_call_revisions = max_tool_call_revisions
        self._evaluation_traces = evaluation_traces
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
        self._tool_evaluation_task: asyncio.Task[ToolCallGateDecision | None] | None = None
        self._pending_tool_turn_id: str | None = None
        self._pending_tool_event: RealtimeModelToolCall | None = None
        self._lifecycle_active = False
        self._closing = False
        self._worker_id = f"realtime:{uuid4()}"
        self._idle = asyncio.Event()
        self._idle.set()
        self._command_done = asyncio.Event()
        self._active_command: InteractionCommand | None = None
        self._active_command_deadline: float | None = None
        self._active_command_injected = False
        self._active_command_delivery_done = asyncio.Event()
        self._active_command_delivery_done.set()
        self._turn_id: str | None = None
        self._activity_turn_id: str | None = None
        self._pending_audio_turn_id: str | None = None
        self._source: InteractionSource = "speech_user"
        self._connection_state: RealtimeConnectionState = "connected"
        self._accepting_audio = False
        self._turn_has_input = False
        self._turn_has_output = False
        self._input_parts: list[str] = []
        self._output_parts: list[str] = []
        self._pre_tool_output: str | None = None
        self._deferred_output_parts: list[str] = []
        self._deferred_output_events: list[RealtimeOutputTranscriptDelta | RealtimeAudioDelta] = []
        self._deferred_audio_bytes = 0
        self._turn_items: list[ConversationItem] = []
        self._records: list[ToolCallRecord] = []
        self._visual_components: list[VisualPresentation] = []
        self._pending_visual_components: list[VisualPresentation] = []
        self._visual_component_ready = asyncio.Event()
        self._model_calls: list[ModelCallMetrics] = []
        self._tool_rounds = 0
        self._tool_call_revisions = 0
        self._evaluation_attempt = 0
        self._terminal_rejection_sent = False

    @property
    def connection_state(self) -> RealtimeConnectionState:
        """Expose the neutral lifecycle state without provider recovery details."""
        return self._connection_state

    @asynccontextmanager
    async def lifecycle(self) -> AsyncGenerator[None, None]:
        """Own writer and durable dispatcher tasks for the socket lifetime."""
        self._lifecycle_active = True
        self._closing = False
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
        """Open microphone capture and let provider VAD begin the speech turn."""
        if self._accepting_audio:
            raise RealtimeSessionStateError("An audio input stream is already active")
        if not turn_id:
            raise RealtimeSessionStateError("turn_id cannot be empty")
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
        await self._enqueue(
            "audio",
            AudioChunk(data=data, mime_type=self._input_audio_mime_type),
            audio_bytes=len(data),
        )

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
        receive_task: asyncio.Task[RealtimeModelEvent] = asyncio.create_task(
            self._next_model_event(iterator)
        )
        visual_task: asyncio.Task[bool] | None = None
        try:
            while True:
                visual_task = asyncio.create_task(self._visual_component_ready.wait())
                waiters: set[asyncio.Task[Any]] = {receive_task, visual_task}
                if self._dispatcher_task is not None:
                    waiters.add(self._dispatcher_task)
                tool_evaluation_task = self._tool_evaluation_task
                if tool_evaluation_task is not None:
                    waiters.add(tool_evaluation_task)
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if self._dispatcher_task is not None and self._dispatcher_task in done:
                    exception = self._dispatcher_task.exception()
                    if exception is not None:
                        raise exception
                    raise RealtimeSessionStateError("Realtime command dispatcher stopped")
                if tool_evaluation_task is not None and tool_evaluation_task in done:
                    visual_task.cancel()
                    await asyncio.gather(visual_task, return_exceptions=True)
                    visual_task = None
                    async for tool_event in self._settle_tool_evaluation():
                        yield tool_event
                    continue
                if self._pending_visual_components:
                    pending = tuple(self._pending_visual_components)
                    self._pending_visual_components = []
                    self._visual_component_ready.clear()
                    for presentation in pending:
                        yield RealtimeVisualComponent(
                            turn_id=self._ensure_turn_id(),
                            presentation=presentation,
                        )
                    continue
                visual_task.cancel()
                await asyncio.gather(visual_task, return_exceptions=True)
                visual_task = None
                try:
                    event = receive_task.result()
                except StopAsyncIteration:
                    if self._tool_evaluation_task is not None:
                        async for tool_event in self._settle_tool_evaluation():
                            yield tool_event
                    if not self._lifecycle_active:
                        await self._close_tasks()
                    return
                async for normalized in self._handle_model_event(event):
                    yield normalized
                receive_task = asyncio.create_task(self._next_model_event(iterator))
        finally:
            receive_task.cancel()
            if visual_task is not None:
                visual_task.cancel()
            await asyncio.gather(
                *(task for task in (receive_task, visual_task) if task is not None),
                return_exceptions=True,
            )
            await self._cancel_tool_evaluation()

    @staticmethod
    async def _next_model_event(
        iterator: AsyncIterator[RealtimeModelEvent],
    ) -> RealtimeModelEvent:
        """Wrap an async iterator awaitable for task-based failure monitoring."""
        return await anext(iterator)

    async def _handle_model_event(self, event: object) -> AsyncIterator[RealtimeAgentEvent]:
        """Translate one provider-neutral event and advance connection-local state."""
        if self._tool_evaluation_task is not None and isinstance(
            event,
            (
                RealtimeModelInputTranscriptDelta,
                RealtimeModelAudioInterrupted,
                RealtimeModelActivityStarted,
            ),
        ):
            # Output may keep flowing while policy runs, but events that can replace
            # the logical turn must settle its pending call first.
            async for tool_event in self._settle_tool_evaluation():
                yield tool_event
        if self._pending_visual_components:
            turn_id = self._ensure_turn_id()
            pending = tuple(self._pending_visual_components)
            self._pending_visual_components = []
            self._visual_component_ready.clear()
            for presentation in pending:
                yield RealtimeVisualComponent(turn_id=turn_id, presentation=presentation)
        if isinstance(event, RealtimeModelInputTranscriptDelta):
            # OpenAI input transcription is asynchronous and may arrive after output
            # has already started. In that case it still belongs to the active turn.
            if self._turn_id is None or self._active_command is not None:
                await self._activate_audio_turn_for_input()
            turn_id = self._require_turn_id()
            self._input_parts.append(event.text)
            self._turn_has_input = True
            yield RealtimeInputTranscriptDelta(turn_id=turn_id, text=event.text)
        elif isinstance(event, RealtimeModelOutputTranscriptDelta):
            turn_id = self._ensure_turn_id()
            self._turn_has_output = True
            transcript_event = RealtimeOutputTranscriptDelta(turn_id=turn_id, text=event.text)
            if self._pre_tool_output is not None:
                self._deferred_output_parts.append(event.text)
                self._deferred_output_events.append(transcript_event)
            else:
                self._output_parts.append(event.text)
                yield transcript_event
        elif isinstance(event, RealtimeModelAudioDelta):
            turn_id = self._ensure_turn_id()
            self._turn_has_output = True
            audio_event = RealtimeAudioDelta(
                turn_id=turn_id,
                data=event.data,
                mime_type=event.mime_type,
            )
            if self._pre_tool_output is not None:
                self._deferred_output_events.append(audio_event)
                self._deferred_audio_bytes += len(event.data)
                if self._deferred_audio_bytes > MAX_DEFERRED_REALTIME_AUDIO_BYTES:
                    logger.warning(
                        "realtime_tool_continuation_buffer_exceeded",
                        turn_id=turn_id,
                        audio_bytes=self._deferred_audio_bytes,
                    )
                    for deferred in self._release_deferred_output(deduplicate=False):
                        yield deferred
            else:
                yield audio_event
        elif isinstance(event, RealtimeModelAudioInterrupted):
            interrupted_turn_id = await self._activate_audio_turn_for_input()
            yield RealtimeAudioInterrupted(turn_id=interrupted_turn_id or self._ensure_turn_id())
        elif isinstance(event, RealtimeModelToolCall):
            self._capture_metrics(event.usage)
            self._schedule_tool_evaluation(self._ensure_turn_id(), event)
        elif isinstance(event, RealtimeModelTurnCompleted):
            if self._tool_evaluation_task is not None:
                async for tool_event in self._settle_tool_evaluation():
                    yield tool_event
                return
            turn_id = self._ensure_turn_id()
            self._capture_metrics(event.usage)
            for deferred in self._release_deferred_output(deduplicate=True):
                yield deferred
            yield await self._complete_turn(turn_id, event.response_id)
        elif isinstance(event, RealtimeModelActivityStarted):
            await self._activate_audio_turn_for_input()
            self._activity_turn_id = self._ensure_turn_id()
            yield RealtimeActivityStarted(turn_id=self._activity_turn_id)
        elif isinstance(event, RealtimeModelActivityEnded):
            turn_id = (
                self._activity_turn_id
                or self._turn_id
                or self._pending_audio_turn_id
                or str(uuid4())
            )
            self._activity_turn_id = None
            yield RealtimeActivityEnded(turn_id=turn_id)
        elif isinstance(event, RealtimeModelReconnectRequested):
            self._connection_state = "recovering"
            yield RealtimeReconnectRequested(deadline_seconds=event.deadline_seconds)
        elif isinstance(event, RealtimeModelReconnected):
            self._connection_state = "connected"
            yield RealtimeReconnected(resumed=event.resumed)

    def _schedule_tool_evaluation(
        self,
        turn_id: str,
        event: RealtimeModelToolCall,
    ) -> None:
        """Start policy evaluation without blocking subsequent provider output events."""
        if self._tool_evaluation_task is not None:
            raise RealtimeSessionStateError(
                "Realtime model emitted another tool batch while evaluation was pending"
            )
        if self._pre_tool_output is None:
            emitted = "".join(self._output_parts).strip()
            if emitted:
                self._pre_tool_output = emitted
        self._evaluation_attempt += 1
        self._pending_tool_turn_id = turn_id
        self._pending_tool_event = event
        self._tool_evaluation_task = asyncio.create_task(
            self._inspect_tool_calls(
                turn_id=turn_id,
                calls=event.calls,
                attempt=self._evaluation_attempt,
            ),
            name=f"realtime-tool-evaluation-{turn_id}",
        )

    async def _settle_tool_evaluation(self) -> AsyncIterator[RealtimeAgentEvent]:
        """Apply one completed background decision before any external tool effect."""
        task = self._tool_evaluation_task
        turn_id = self._pending_tool_turn_id
        event = self._pending_tool_event
        if task is None or turn_id is None or event is None:
            raise RealtimeSessionStateError("Realtime tool evaluation state is incomplete")
        try:
            decision = await task
        finally:
            self._tool_evaluation_task = None
            self._pending_tool_turn_id = None
            self._pending_tool_event = None
        async for tool_event in self._finish_tools(turn_id, event, decision):
            yield tool_event

    async def _finish_tools(
        self,
        turn_id: str,
        event: RealtimeModelToolCall,
        decision: ToolCallGateDecision | None,
    ) -> AsyncIterator[RealtimeAgentEvent]:
        """Apply evaluation policy, then execute only an approved realtime batch."""
        if self._terminal_rejection_sent:
            raise RealtimeToolCallEvaluationRejectedError(
                "Realtime agent requested another tool after a terminal evaluation result"
            )
        if decision is not None and not decision.execute:
            feedback_results = decision.feedback_results
            if decision.disposition == "inform_user":
                self._terminal_rejection_sent = True
            elif self._tool_call_revisions >= self._max_tool_call_revisions:
                feedback_results = tool_call_revision_limit_results(event.calls)
                self._terminal_rejection_sent = True
            else:
                self._tool_call_revisions += 1
            self._turn_items.extend(event.calls)
            self._turn_items.extend(feedback_results)
            await self._enqueue("tool_results", feedback_results)
            return
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
        execution = await self._tool_executor.execute(
            event.calls,
            self._tools,
            ToolExecutionContext.from_conversation(
                self._conversation_key,
                delivery_mode="realtime",
            ),
        )
        self._records.extend(execution.records)
        extend_visual_components(self._visual_components, execution.visual_components)
        self._turn_items.extend(event.calls)
        self._turn_items.extend(execution.results)
        for record in execution.records:
            yield RealtimeToolCompleted(turn_id=turn_id, record=record)
        for presentation in execution.visual_components:
            yield RealtimeVisualComponent(turn_id=turn_id, presentation=presentation)
        await self._enqueue("tool_results", execution.results)

    async def _cancel_tool_evaluation(self) -> None:
        """Cancel and clear a pending evaluator task when its realtime session closes."""
        task = self._tool_evaluation_task
        self._tool_evaluation_task = None
        self._pending_tool_turn_id = None
        self._pending_tool_event = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _inspect_tool_calls(
        self,
        *,
        turn_id: str,
        calls: tuple[ToolCall, ...],
        attempt: int,
    ) -> ToolCallGateDecision | None:
        """Evaluate a realtime batch against durable history and current-turn evidence."""
        if self._tool_call_gate is None:
            return None
        conversation = await self._conversations.load(self._conversation_key)
        if conversation is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        current_input = "".join(self._input_parts).strip()
        current_items: tuple[ConversationItem, ...] = tuple(self._turn_items)
        if current_input:
            current_items = (
                ConversationMessage(role="user", content=current_input, source=self._source),
                *current_items,
            )
        decision = await self._tool_call_gate.inspect(
            context=conversation.messages + current_items,
            available_tools=self._tools.specs,
            proposed_calls=calls,
        )
        if decision.evaluation is not None:
            self._capture_metrics(
                decision.evaluation.usage,
                model=decision.evaluation.model,
            )
            await append_evaluation_trace(
                self._evaluation_traces,
                decision=decision,
                conversation_id=self._conversation_key.conversation_id,
                turn_id=turn_id,
                job_id=None,
                attempt=attempt,
                calls=calls,
            )
        return decision

    def _release_deferred_output(
        self,
        *,
        deduplicate: bool,
    ) -> tuple[RealtimeOutputTranscriptDelta | RealtimeAudioDelta, ...]:
        """Release a post-tool continuation unless it repeats prior spoken output."""
        if self._pre_tool_output is None:
            return ()
        continuation = "".join(self._deferred_output_parts).strip()
        duplicate = (
            deduplicate
            and bool(continuation)
            and self._normalized_spoken_text(continuation)
            == self._normalized_spoken_text(self._pre_tool_output)
        )
        events = () if duplicate else tuple(self._deferred_output_events)
        if duplicate:
            logger.info(
                "realtime_duplicate_tool_continuation_suppressed",
                turn_id=self._turn_id,
                audio_bytes=self._deferred_audio_bytes,
            )
        else:
            self._output_parts.extend(self._deferred_output_parts)
        self._pre_tool_output = None
        self._deferred_output_parts = []
        self._deferred_output_events = []
        self._deferred_audio_bytes = 0
        return events

    @staticmethod
    def _normalized_spoken_text(value: str) -> str:
        """Compare spoken copies independently of whitespace and punctuation."""
        return "".join(character for character in value.casefold() if character.isalnum())

    async def _complete_turn(self, turn_id: str, response_id: str) -> RealtimeTurnCompleted:
        """Persist one real provider turn before confirming proactive delivery."""
        answer = "".join(self._output_parts).strip()
        user_text = "".join(self._input_parts).strip()
        source = self._source
        active_command = self._active_command
        metrics = TurnMetrics(calls=tuple(self._model_calls))
        result = AgentResult(
            answer=answer,
            response_id=response_id,
            conversation_id=self._conversation_key.conversation_id,
            tool_calls=tuple(self._records),
            visual_components=tuple(self._visual_components),
            metrics=metrics,
        )
        if user_text:
            turn = (
                ConversationMessage(role="user", content=user_text, source=source),
                *self._turn_items,
                ConversationMessage(
                    role="assistant",
                    content=answer,
                    source="assistant",
                    metrics=metrics if metrics.calls else None,
                ),
            )
            await self._persist_turn(turn, turn_id=turn_id)
        if active_command is not None and active_command.request_id == turn_id:
            assert self._interactions is not None
            await self._interactions.complete(active_command.command_id, self._worker_id)
            self._active_command = None
            self._active_command_deadline = None
            self._active_command_injected = False
            self._active_command_delivery_done.set()
            self._command_done.set()
        self._turn_id = None
        self._reset_turn_buffers()
        self._refresh_idle()
        logger.info(
            "realtime_turn_completed",
            response_id=response_id,
            turn_id=turn_id,
            input_tokens=metrics.usage.input_tokens,
            output_tokens=metrics.usage.output_tokens,
            cached_input_tokens=metrics.usage.cached_input_tokens,
            total_tokens=metrics.usage.total_tokens,
            cost_amount=str(metrics.cost.amount) if metrics.cost is not None else None,
            cost_currency=metrics.cost.currency if metrics.cost is not None else None,
        )
        return RealtimeTurnCompleted(
            turn_id=turn_id,
            result=result,
            source=source,
            job_id=active_command.request_id if active_command is not None else None,
            causation_id=active_command.causation_id if active_command is not None else None,
        )

    async def _persist_interrupted_user_turn(self) -> str | None:
        """Persist the client-visible prefix of a user turn before discarding it."""
        turn_id = self._turn_id
        if turn_id is None or self._source == "worker_agent":
            return None
        answer = "".join(self._output_parts).strip()
        user_text = "".join(self._input_parts).strip()
        metrics = TurnMetrics(calls=tuple(self._model_calls))
        if user_text:
            turn = (
                ConversationMessage(role="user", content=user_text, source=self._source),
                *self._turn_items,
                ConversationMessage(
                    role="assistant",
                    content=answer,
                    source="assistant",
                    metrics=metrics if metrics.calls else None,
                ),
            )
            await self._persist_turn(turn, turn_id=turn_id)
        self._turn_id = None
        self._reset_turn_buffers()
        self._refresh_idle()
        logger.info(
            "realtime_turn_interrupted",
            turn_id=turn_id,
            persisted=bool(user_text),
            input_tokens=metrics.usage.input_tokens,
            output_tokens=metrics.usage.output_tokens,
            cached_input_tokens=metrics.usage.cached_input_tokens,
            total_tokens=metrics.usage.total_tokens,
        )
        return turn_id

    def _capture_metrics(self, usage: ModelUsage, *, model: str | None = None) -> None:
        """Accumulate each billable realtime response inside its logical turn."""
        if usage.total_tokens:
            self._model_calls.append(self._cost_calculator.metrics(model or self._model, usage))

    async def _persist_turn(
        self,
        turn: tuple[ConversationItem, ...],
        *,
        turn_id: str,
    ) -> None:
        """Append against the latest version and retry one concurrent write."""
        for attempt in range(2):
            conversation = await self._conversations.load(self._conversation_key)
            if conversation is None:
                raise ConversationNotFoundError("Conversation session does not exist")
            try:
                await self._conversations.save_turn(conversation, turn, turn_id=turn_id)
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
            while not self._closing:
                await self._idle.wait()
                if self._closing:
                    return
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
                self._active_command_deadline = (
                    asyncio.get_running_loop().time() + self._proactive_turn_timeout_seconds
                )
                self._active_command_injected = False
                self._active_command_delivery_done.clear()
                self._command_done.clear()
                if self._closing:
                    await self._requeue_active_command()
                    return
                self._begin_turn(
                    command.request_id,
                    source="worker_agent",
                    preserve_pending_audio=self._accepting_audio,
                )
                self._input_parts.append(command.message)
                inherited_visual_list: list[VisualPresentation] = []
                extend_visual_components(
                    inherited_visual_list,
                    visual_components_from_completion_message(command.message),
                )
                inherited_visuals = tuple(inherited_visual_list)
                extend_visual_components(self._visual_components, inherited_visuals)
                self._pending_visual_components.extend(inherited_visuals)
                if inherited_visuals:
                    self._visual_component_ready.set()
                try:
                    await self._enqueue("a2a_completion", command.message)
                    if self._active_command is None:
                        continue
                    remaining = self._active_command_time_remaining()
                    if remaining <= 0:
                        raise TimeoutError
                    await asyncio.wait_for(
                        self._command_done.wait(),
                        timeout=remaining,
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
        elif kind == "text":
            assert isinstance(payload, str)
            await self._model_session.send_text(payload)
        elif kind == "a2a_completion":
            assert isinstance(payload, str)
            try:
                await self._model_session.send_text(payload)
                command = self._active_command
                if command is not None and command.message == payload:
                    self._active_command_injected = True
            finally:
                self._active_command_delivery_done.set()
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
        self._closing = True
        await self._cancel_tool_evaluation()
        try:
            await self._drain_visible_proactive_turn()
        finally:
            try:
                await self._requeue_active_command()
                await self._persist_interrupted_user_turn()
            finally:
                if self._dispatcher_task is not None:
                    self._dispatcher_task.cancel()
                    await asyncio.gather(self._dispatcher_task, return_exceptions=True)
                await self._stop_writer()
                self._dispatcher_task = None
                self._writer_task = None

    async def _drain_visible_proactive_turn(self) -> None:
        """Finish a response already exposed to the client before closing its session."""
        command = self._active_command
        if command is None or not self._turn_has_output:
            return
        remaining = self._active_command_time_remaining()
        if remaining <= 0:
            return
        try:
            async with asyncio.timeout(remaining):
                async for event in self._model_session.receive():
                    async for _ in self._handle_model_event(event):
                        pass
                    if self._active_command is None:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "realtime_visible_turn_drain_failed",
                command_id=command.command_id,
                error_type=type(exc).__name__,
            )

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
        self._active_command_deadline = None
        self._active_command_injected = False
        self._active_command_delivery_done.set()
        self._command_done.set()
        try:
            await asyncio.shield(self._interactions.requeue(command.command_id, self._worker_id))
        except Exception as exc:
            logger.warning(
                "realtime_command_requeue_failed",
                command_id=command.command_id,
                error_type=type(exc).__name__,
            )

    async def _settle_active_command_for_user_interruption(self) -> str | None:
        """Acknowledge an injected result instead of scheduling the same notice again."""
        command = self._active_command
        if command is None:
            return None
        turn_id = command.request_id
        if not self._active_command_delivery_done.is_set():
            try:
                await asyncio.wait_for(
                    self._active_command_delivery_done.wait(),
                    timeout=self._outbound_enqueue_timeout_seconds,
                )
            except TimeoutError as exc:
                raise RealtimeBackpressureError(
                    "Realtime proactive delivery did not finish before interruption"
                ) from exc
            if self._active_command is None:
                return turn_id
        if not self._active_command_injected:
            await self._requeue_active_command()
            return turn_id
        assert self._interactions is not None
        metrics = TurnMetrics(calls=tuple(self._model_calls))
        turn = (
            ConversationMessage(role="user", content=command.message, source="worker_agent"),
            *self._turn_items,
            ConversationMessage(
                role="assistant",
                content="".join(self._output_parts).strip(),
                source="assistant",
                metrics=metrics if metrics.calls else None,
            ),
        )
        await self._persist_turn(turn, turn_id=turn_id)
        await self._interactions.complete(command.command_id, self._worker_id)
        self._active_command = None
        self._active_command_deadline = None
        self._active_command_injected = False
        self._active_command_delivery_done.set()
        self._command_done.set()
        self._turn_id = None
        self._reset_turn_buffers()
        self._refresh_idle()
        logger.info(
            "realtime_proactive_turn_interrupted",
            command_id=command.command_id,
            turn_id=turn_id,
            persisted=True,
        )
        return turn_id

    def _active_command_time_remaining(self) -> float:
        """Return the bounded time left for the current proactive model turn."""
        if self._active_command_deadline is None:
            return 0.0
        return max(
            0.0,
            self._active_command_deadline - asyncio.get_running_loop().time(),
        )

    def _begin_turn(
        self,
        turn_id: str,
        *,
        source: InteractionSource,
        preserve_pending_audio: bool = False,
    ) -> None:
        """Start a logical turn after its predecessor has been durably settled."""
        if not turn_id:
            raise RealtimeSessionStateError("turn_id cannot be empty")
        self._turn_id = turn_id
        if not preserve_pending_audio:
            self._pending_audio_turn_id = None
        self._source = source
        self._reset_turn_buffers()
        self._refresh_idle()

    async def _activate_audio_turn_for_input(self) -> str | None:
        """Supersede an active turn, retaining any user response already exposed."""
        interrupted_turn_id: str | None = None
        if self._pending_audio_turn_id is not None:
            if self._active_command is not None:
                interrupted_turn_id = await self._settle_active_command_for_user_interruption()
            else:
                interrupted_turn_id = await self._persist_interrupted_user_turn()
            self._begin_turn(self._pending_audio_turn_id, source="speech_user")
        elif self._active_command is not None:
            interrupted_turn_id = await self._settle_active_command_for_user_interruption()
            self._begin_turn(str(uuid4()), source="speech_user")
        elif self._turn_id is None:
            self._begin_turn(str(uuid4()), source="speech_user")
        elif self._turn_has_output:
            interrupted_turn_id = await self._persist_interrupted_user_turn()
            self._begin_turn(str(uuid4()), source="speech_user")
        return interrupted_turn_id

    def _reset_turn_buffers(self) -> None:
        """Discard connection-local transcript and audit buffers between turns."""
        self._input_parts = []
        self._output_parts = []
        self._pre_tool_output = None
        self._deferred_output_parts = []
        self._deferred_output_events = []
        self._deferred_audio_bytes = 0
        self._turn_items = []
        self._records = []
        self._visual_components = []
        self._pending_visual_components = []
        self._visual_component_ready.clear()
        self._model_calls = []
        self._tool_rounds = 0
        self._tool_call_revisions = 0
        self._evaluation_attempt = 0
        self._terminal_rejection_sent = False
        self._turn_has_input = False
        self._turn_has_output = False

    def _refresh_idle(self) -> None:
        """Wake the dispatcher when provider VAD has no logical turn in flight."""
        if self._turn_id is None:
            self._idle.set()
        else:
            self._idle.clear()

    def _ensure_turn_id(self) -> str:
        """Create a server turn ID for provider output lacking explicit input."""
        if self._turn_id is None:
            turn_id = self._pending_audio_turn_id or str(uuid4())
            self._begin_turn(turn_id, source="speech_user")
        return self._require_turn_id()

    def _require_turn_id(self) -> str:
        """Reject provider output that is not associated with a logical turn."""
        if self._turn_id is None:
            raise RealtimeSessionStateError("Realtime model emitted output without an active turn")
        return self._turn_id
