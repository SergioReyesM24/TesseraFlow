import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import structlog
from google import genai
from google.genai import types

from application.ports import RealtimeModelGateway, RealtimeModelSession
from domain.agent import AgentDefinition
from domain.conversations import ConversationItem
from domain.realtime import (
    AudioChunk,
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
    RealtimeSessionCapabilities,
    RealtimeSessionOptions,
)
from domain.tools import ToolCall, ToolResult, ToolSpec
from infrastructure.gemini_live_gateway import (
    GeminiLiveGateway,
    GeminiLiveProtocolError,
    GeminiLiveSessionStateError,
)

logger = structlog.get_logger(__name__)


class GeminiRealtimeGateway(RealtimeModelGateway):
    """Open resilient Gemini STS sessions behind neutral realtime contracts."""

    def __init__(
        self,
        client: genai.Client,
        *,
        model: str = "gemini-3.1-flash-live-preview",
        voice_name: str,
        input_language_code: str | None,
        max_resumption_attempts: int = 3,
        resumption_timeout_seconds: float = 15.0,
    ) -> None:
        """Store immutable provider configuration and recovery budgets."""
        self._client = client
        self._model = model
        self._voice_name = voice_name
        self._input_language_code = input_language_code
        self._max_resumption_attempts = max_resumption_attempts
        self._resumption_timeout_seconds = resumption_timeout_seconds

    @property
    def capabilities(self) -> RealtimeSessionCapabilities:
        """Describe Gemini features using provider-neutral values."""
        return RealtimeSessionCapabilities(
            input_audio_mime_type="audio/pcm;rate=16000",
            output_audio_mime_type="audio/pcm;rate=24000",
            activity_detection_modes=("automatic", "explicit"),
            supports_barge_in=True,
            recovery_mode="transparent",
        )

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
        options: RealtimeSessionOptions,
    ) -> AsyncIterator[RealtimeModelSession]:
        """Connect a recoverable session and keep SDK lifecycle in infrastructure."""
        config = self._config(definition, tools, options, handle=None)
        session = GeminiRealtimeModelSession(
            client=self._client,
            model=self._model or definition.model,
            base_config=config,
            history=history,
            max_resumption_attempts=self._max_resumption_attempts,
            resumption_timeout_seconds=self._resumption_timeout_seconds,
        )
        await session.start()
        try:
            yield session
        finally:
            await session.close()

    def _config(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        options: RealtimeSessionOptions,
        *,
        handle: str | None,
    ) -> types.LiveConnectConfig:
        """Translate neutral setup options into Gemini connection configuration."""
        activity = options.activity
        automatic = types.AutomaticActivityDetection(
            disabled=activity.detection == "explicit",
            start_of_speech_sensitivity=self._start_sensitivity(activity.start_sensitivity),
            end_of_speech_sensitivity=self._end_sensitivity(activity.end_sensitivity),
            prefix_padding_ms=activity.prefix_padding_ms,
            silence_duration_ms=activity.silence_duration_ms,
        )
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=definition.instructions,
            speech_config=types.SpeechConfig(
                language_code=self._input_language_code,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice_name)
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=automatic,
                activity_handling=(
                    types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
                    if activity.interrupt_on_activity
                    else types.ActivityHandling.NO_INTERRUPTION
                ),
            ),
            session_resumption=types.SessionResumptionConfig(handle=handle),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            history_config=types.HistoryConfig(initial_history_in_client_content=True),
            tools=[
                types.Tool(
                    function_declarations=[
                        GeminiLiveGateway._to_function_declaration(tool) for tool in tools
                    ]
                )
            ]
            if tools
            else None,
        )

    @staticmethod
    def _start_sensitivity(value: str | None) -> types.StartSensitivity | None:
        """Map a neutral optional sensitivity to the Gemini enum."""
        if value == "high":
            return types.StartSensitivity.START_SENSITIVITY_HIGH
        if value == "low":
            return types.StartSensitivity.START_SENSITIVITY_LOW
        return None

    @staticmethod
    def _end_sensitivity(value: str | None) -> types.EndSensitivity | None:
        """Map a neutral optional sensitivity to the Gemini enum."""
        if value == "high":
            return types.EndSensitivity.END_SENSITIVITY_HIGH
        if value == "low":
            return types.EndSensitivity.END_SENSITIVITY_LOW
        return None


class GeminiRealtimeModelSession(RealtimeModelSession):
    """Translate one Gemini connection and transparently recover resumable state."""

    def __init__(
        self,
        session: genai.live.AsyncSession | None = None,
        *,
        client: genai.Client | None = None,
        model: str | None = None,
        base_config: types.LiveConnectConfig | None = None,
        history: tuple[ConversationItem, ...] = (),
        max_resumption_attempts: int = 0,
        resumption_timeout_seconds: float = 15.0,
    ) -> None:
        """Support direct test sessions and gateway-owned resilient sessions."""
        self._session = session
        self._client = client
        self._model = model
        self._base_config = base_config
        self._history = history
        self._max_resumption_attempts = max_resumption_attempts
        self._resumption_timeout_seconds = resumption_timeout_seconds
        self._connection_context: AbstractAsyncContextManager[genai.live.AsyncSession] | None = None
        self._messages: AsyncIterator[types.LiveServerMessage] | None = None
        self._pending_calls: dict[str, str] = {}
        self._resume_handle: str | None = None
        self._ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        if session is not None:
            self._ready.set()

    async def start(self) -> None:
        """Open the initial provider connection and prefill retained history once."""
        if self._session is not None:
            return
        await self._connect(handle=None, include_history=True)

    async def close(self) -> None:
        """Release the currently owned SDK connection idempotently."""
        self._ready.clear()
        context = self._connection_context
        self._connection_context = None
        self._session = None
        self._messages = None
        if context is not None:
            await context.__aexit__(None, None, None)

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send raw PCM through Gemini's low-latency realtime input channel."""
        await self._send("audio", chunk)

    async def end_audio(self) -> None:
        """Tell automatic VAD that the current capture stream ended."""
        await self._send("audio_end", None)

    async def start_activity(self) -> None:
        """Translate explicit user activity start."""
        await self._send("activity_start", None)

    async def end_activity(self) -> None:
        """Translate explicit user activity end."""
        await self._send("activity_end", None)

    async def send_text(self, text: str) -> None:
        """Send a textual turn through the same low-latency connection."""
        await self._send("text", text)

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Validate and send one complete pending function-response batch."""
        result_ids = [result.call_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise GeminiLiveSessionStateError("Tool result call IDs must be unique")
        if set(result_ids) != set(self._pending_calls):
            raise GeminiLiveSessionStateError(
                "Tool results must match all pending Gemini realtime calls"
            )
        responses = tuple(
            types.FunctionResponse(
                id=result.call_id,
                name=self._pending_calls[result.call_id],
                response=GeminiLiveGateway._tool_result_payload(result),
            )
            for result in results
        )
        await self._send("tool_results", responses)
        self._pending_calls = {}

    async def receive(self) -> AsyncIterator[RealtimeModelEvent]:
        """Continuously normalize server events across transparent reconnects."""
        while True:
            try:
                message = await self._next_message()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._resume_handle is None:
                    raise
                self._ready.clear()
                yield RealtimeModelReconnectRequested()
                await self._recover()
                yield RealtimeModelReconnected(resumed=True)
                continue

            update = message.session_resumption_update
            if update is not None:
                self._apply_resumption_update(update)

            go_away = message.go_away
            if go_away is not None:
                self._ready.clear()
                yield RealtimeModelReconnectRequested(
                    deadline_seconds=self._duration_seconds(go_away.time_left)
                )
                await self._recover()
                yield RealtimeModelReconnected(resumed=True)
                continue

            activity = message.voice_activity
            if activity is not None:
                if activity.voice_activity_type == types.VoiceActivityType.ACTIVITY_START:
                    yield RealtimeModelActivityStarted()
                elif activity.voice_activity_type == types.VoiceActivityType.ACTIVITY_END:
                    yield RealtimeModelActivityEnded()
            vad = message.voice_activity_detection_signal
            if vad is not None:
                if vad.vad_signal_type == types.VadSignalType.VAD_SIGNAL_TYPE_SOS:
                    yield RealtimeModelActivityStarted()
                elif vad.vad_signal_type == types.VadSignalType.VAD_SIGNAL_TYPE_EOS:
                    yield RealtimeModelActivityEnded()

            server_content = message.server_content
            if server_content is not None:
                if server_content.interrupted:
                    yield RealtimeModelAudioInterrupted()
                model_turn = server_content.model_turn
                if model_turn is not None:
                    for part in model_turn.parts or []:
                        inline_data = part.inline_data
                        if inline_data is None or inline_data.data is None:
                            continue
                        yield RealtimeModelAudioDelta(
                            data=inline_data.data,
                            mime_type=inline_data.mime_type or "audio/pcm;rate=24000",
                        )
                input_transcription = server_content.input_transcription
                if input_transcription is not None and input_transcription.text:
                    yield RealtimeModelInputTranscriptDelta(text=input_transcription.text)
                output_transcription = server_content.output_transcription
                if output_transcription is not None and output_transcription.text:
                    yield RealtimeModelOutputTranscriptDelta(text=output_transcription.text)

            tool_call = message.tool_call
            if tool_call is not None:
                calls = tuple(self._normalize_call(call) for call in tool_call.function_calls or [])
                if calls:
                    if self._pending_calls:
                        raise GeminiLiveSessionStateError(
                            "Gemini emitted new realtime calls before prior results returned"
                        )
                    self._pending_calls = {call.call_id: call.tool_name for call in calls}
                    yield RealtimeModelToolCall(calls=calls)

            if server_content is not None and server_content.turn_complete:
                yield RealtimeModelTurnCompleted(response_id=str(uuid4()))

    async def _send(self, kind: str, payload: object) -> None:
        """Serialize one write and block while session recovery is active."""
        while True:
            await self._ready.wait()
            async with self._send_lock:
                if not self._ready.is_set():
                    continue
                await self._send_raw(kind, payload)
                return

    async def _send_raw(self, kind: str, payload: object) -> None:
        """Translate one realtime operation to exactly one SDK write."""
        session = self._require_session()
        if kind == "audio":
            assert isinstance(payload, AudioChunk)
            await session.send_realtime_input(
                audio=types.Blob(data=payload.data, mime_type=payload.mime_type)
            )
        elif kind == "audio_end":
            await session.send_realtime_input(audio_stream_end=True)
        elif kind == "activity_start":
            await session.send_realtime_input(activity_start=types.ActivityStart())
        elif kind == "activity_end":
            await session.send_realtime_input(activity_end=types.ActivityEnd())
        elif kind == "text":
            assert isinstance(payload, str)
            await session.send_realtime_input(text=payload)
        elif kind == "tool_results":
            await session.send_tool_response(function_responses=cast(Any, payload))
        else:
            raise AssertionError(f"Unknown Gemini realtime write: {kind}")

    async def _connect(self, *, handle: str | None, include_history: bool) -> None:
        """Open one SDK connection using an optional provider-owned resume handle."""
        if self._client is None or self._model is None or self._base_config is None:
            raise GeminiLiveSessionStateError("Session cannot reconnect without its gateway")
        config = self._base_config.model_copy(
            update={"session_resumption": types.SessionResumptionConfig(handle=handle)}
        )
        context = self._client.aio.live.connect(model=self._model, config=config)
        provider_session = await context.__aenter__()
        self._connection_context = context
        self._session = provider_session
        self._messages = None
        if include_history:
            prefill = GeminiLiveGateway._to_history(self._history)
            if prefill:
                await provider_session.send_client_content(
                    turns=cast(Any, prefill),
                    turn_complete=False,
                )
        self._ready.set()
        logger.info("gemini_realtime_session_connected", resumed=handle is not None)

    async def _recover(self) -> None:
        """Reconnect with the latest provider-owned session handle."""
        if self._resume_handle is None or self._max_resumption_attempts < 1:
            raise GeminiLiveProtocolError("Gemini realtime session is not resumable")
        self._ready.clear()
        last_error: Exception | None = None
        async with self._send_lock:
            for attempt in range(1, self._max_resumption_attempts + 1):
                try:
                    async with asyncio.timeout(self._resumption_timeout_seconds):
                        await self._replace_connection(self._resume_handle)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "gemini_realtime_resumption_failed",
                        attempt=attempt,
                        error_type=type(exc).__name__,
                    )
                    if attempt < self._max_resumption_attempts:
                        await asyncio.sleep(min(0.25 * 2 ** (attempt - 1), 2.0))
                else:
                    self._ready.set()
                    return
        raise GeminiLiveProtocolError("Gemini realtime resumption budget exhausted") from last_error

    async def _replace_connection(self, handle: str) -> None:
        """Close the old socket and resume its provider-managed session."""
        context = self._connection_context
        self._connection_context = None
        self._session = None
        self._messages = None
        if context is not None:
            await context.__aexit__(None, None, None)
        await self._connect(handle=handle, include_history=False)

    def _apply_resumption_update(
        self,
        update: types.LiveServerSessionResumptionUpdate,
    ) -> None:
        """Retain the latest provider handle while the session is resumable."""
        if update.resumable and update.new_handle:
            self._resume_handle = update.new_handle

    async def _next_message(self) -> types.LiveServerMessage:
        """Reopen SDK receive iterators while preserving the live connection."""
        while True:
            if self._messages is None:
                self._messages = self._require_session().receive()
            try:
                return await anext(self._messages)
            except StopAsyncIteration:
                self._messages = None

    def _require_session(self) -> genai.live.AsyncSession:
        """Return the active SDK session or reject an invalid lifecycle call."""
        if self._session is None:
            raise GeminiLiveSessionStateError("Gemini realtime session is disconnected")
        return self._session

    @staticmethod
    def _normalize_call(call: types.FunctionCall) -> ToolCall:
        """Validate one complete Gemini function call and its JSON arguments."""
        if not call.id or not call.name:
            raise GeminiLiveProtocolError("Gemini emitted a tool call without ID or name")
        arguments = call.args or {}
        if not isinstance(arguments, dict):
            raise GeminiLiveProtocolError(f"Tool {call.name} arguments must be a JSON object")
        return ToolCall(call_id=call.id, tool_name=call.name, arguments=arguments)

    @staticmethod
    def _duration_seconds(value: str | None) -> float | None:
        """Convert Gemini duration text to a neutral optional number of seconds."""
        if value is None:
            return None
        normalized = value.removesuffix("s")
        try:
            return float(normalized)
        except ValueError:
            return None
