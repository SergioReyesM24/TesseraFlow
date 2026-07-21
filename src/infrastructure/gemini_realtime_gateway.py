from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    RealtimeModelAudioDelta,
    RealtimeModelAudioInterrupted,
    RealtimeModelEvent,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
)
from domain.tools import ToolCall, ToolResult, ToolSpec
from infrastructure.gemini_live_gateway import (
    GeminiLiveGateway,
    GeminiLiveProtocolError,
    GeminiLiveSessionStateError,
)

logger = structlog.get_logger(__name__)


class GeminiRealtimeGateway(RealtimeModelGateway):
    """Open persistent speech-to-speech sessions over one shared Gemini client."""

    def __init__(
        self,
        client: genai.Client,
        *,
        voice_name: str,
        input_language_code: str | None,
    ) -> None:
        """Store immutable provider configuration without connection-local state."""
        self._client = client
        self._voice_name = voice_name
        self._input_language_code = input_language_code

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncIterator[RealtimeModelSession]:
        """Connect Gemini Live with bidirectional audio and transcript generation."""
        config = types.LiveConnectConfig(
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
        logger.info(
            "gemini_realtime_session_connecting",
            model=definition.model,
            tool_count=len(tools),
            history_item_count=len(history),
        )
        async with self._client.aio.live.connect(
            model=definition.model,
            config=config,
        ) as provider_session:
            prefill = GeminiLiveGateway._to_history(history)
            if prefill:
                await provider_session.send_client_content(
                    turns=cast(Any, prefill),
                    turn_complete=False,
                )
            logger.info("gemini_realtime_session_connected", model=definition.model)
            yield GeminiRealtimeModelSession(provider_session)


class GeminiRealtimeModelSession(RealtimeModelSession):
    """Translate one long-lived Gemini connection to neutral realtime events."""

    def __init__(self, session: genai.live.AsyncSession) -> None:
        """Initialize provider state isolated to one authenticated WebSocket."""
        self._session = session
        self._messages: AsyncIterator[types.LiveServerMessage] | None = None
        self._pending_calls: dict[str, str] = {}

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send raw PCM through Gemini's low-latency realtime input channel."""
        await self._session.send_realtime_input(
            audio=types.Blob(data=chunk.data, mime_type=chunk.mime_type)
        )

    async def end_audio(self) -> None:
        """Tell Gemini that a temporarily paused audio stream has ended."""
        await self._session.send_realtime_input(audio_stream_end=True)

    async def send_text(self, text: str) -> None:
        """Send a fallback textual turn without closing the live connection."""
        await self._session.send_realtime_input(text=text)

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Validate and translate the complete pending function-response batch."""
        result_ids = [result.call_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise GeminiLiveSessionStateError("Tool result call IDs must be unique")
        if set(result_ids) != set(self._pending_calls):
            raise GeminiLiveSessionStateError(
                "Tool results must match all pending Gemini realtime calls"
            )
        responses = [
            types.FunctionResponse(
                id=result.call_id,
                name=self._pending_calls[result.call_id],
                response=GeminiLiveGateway._tool_result_payload(result),
            )
            for result in results
        ]
        await self._session.send_tool_response(function_responses=responses)
        self._pending_calls = {}

    async def receive(self) -> AsyncIterator[RealtimeModelEvent]:
        """Continuously receive all Gemini turns until cancellation or disconnect."""
        while True:
            message = await self._next_message()
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

    async def _next_message(self) -> types.LiveServerMessage:
        """Reopen SDK turn iterators while preserving one long-lived connection."""
        while True:
            if self._messages is None:
                self._messages = self._session.receive()
            try:
                return await anext(self._messages)
            except StopAsyncIteration:
                self._messages = None

    @staticmethod
    def _normalize_call(call: types.FunctionCall) -> ToolCall:
        """Validate one complete Gemini function call and its JSON arguments."""
        if not call.id or not call.name:
            raise GeminiLiveProtocolError("Gemini emitted a tool call without ID or name")
        arguments = call.args or {}
        if not isinstance(arguments, dict):
            raise GeminiLiveProtocolError(f"Tool {call.name} arguments must be a JSON object")
        return ToolCall(call_id=call.id, tool_name=call.name, arguments=arguments)
