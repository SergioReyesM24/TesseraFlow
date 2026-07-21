from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import structlog
from google import genai
from google.genai import types

from application.ports import ModelGateway, ModelSession
from domain.agent import AgentDefinition
from domain.conversations import ConversationItem, ConversationMessage
from domain.model import ModelReply
from domain.tools import ToolCall, ToolResult, ToolSpec
from domain.turn_events import (
    ModelAudioDelta,
    ModelAudioInterrupted,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from domain.types import JsonObject

logger = structlog.get_logger(__name__)


class GeminiLiveProtocolError(RuntimeError):
    """Raised when Gemini Live emits an incomplete or inconsistent interaction."""


class GeminiLiveSessionStateError(RuntimeError):
    """Raised when a caller violates the ordering of live model operations."""


class GeminiLiveGateway(ModelGateway):
    """Open turn-scoped native-audio sessions over one shared Gemini client."""

    def __init__(
        self,
        client: genai.Client,
        *,
        voice_name: str,
        input_language_code: str | None,
    ) -> None:
        """Store immutable voice configuration without retaining user state."""
        self._client = client
        self._voice_name = voice_name
        self._input_language_code = input_language_code

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncIterator[ModelSession]:
        """Translate setup, prefill retained history, and close the live connection."""
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=definition.instructions,
            speech_config=types.SpeechConfig(
                language_code=self._input_language_code,
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice_name)
                ),
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            history_config=types.HistoryConfig(initial_history_in_client_content=True),
            tools=[
                types.Tool(
                    function_declarations=[self._to_function_declaration(tool) for tool in tools]
                )
            ]
            if tools
            else None,
        )
        logger.info(
            "gemini_live_session_connecting",
            model=definition.model,
            tool_count=len(tools),
            history_item_count=len(history),
        )
        async with self._client.aio.live.connect(
            model=definition.model,
            config=config,
        ) as provider_session:
            prefill = self._to_history(history)
            if prefill:
                await provider_session.send_client_content(
                    turns=cast(Any, prefill),
                    turn_complete=False,
                )
            logger.info("gemini_live_session_connected", model=definition.model)
            yield GeminiLiveModelSession(provider_session)

    @staticmethod
    def _to_function_declaration(tool: ToolSpec) -> types.FunctionDeclaration:
        """Translate one closed neutral JSON schema into a Gemini declaration."""
        return types.FunctionDeclaration(
            name=tool.name,
            description=tool.description,
            parameters_json_schema=tool.arguments_schema,
        )

    @classmethod
    def _to_history(cls, history: tuple[ConversationItem, ...]) -> list[types.Content]:
        """Translate retained messages and tool exchanges into Gemini content."""
        contents: list[types.Content] = []
        call_names: dict[str, str] = {}
        for item in history:
            if isinstance(item, ConversationMessage):
                contents.append(
                    types.Content(
                        role="model" if item.role == "assistant" else "user",
                        parts=[types.Part(text=item.content)],
                    )
                )
                continue
            if isinstance(item, ToolCall):
                call_names[item.call_id] = item.tool_name
                contents.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=item.call_id,
                                    name=item.tool_name,
                                    args=item.arguments,
                                )
                            )
                        ],
                    )
                )
                continue
            tool_name = call_names.get(item.call_id)
            if tool_name is None:
                raise GeminiLiveProtocolError(
                    f"Retained tool result {item.call_id} has no preceding tool call"
                )
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=item.call_id,
                                name=tool_name,
                                response=cls._tool_result_payload(item),
                            )
                        )
                    ],
                )
            )
        return contents

    @staticmethod
    def _tool_result_payload(result: ToolResult) -> JsonObject:
        """Build the provider payload for one successful or failed tool result."""
        if result.error is None:
            return {"ok": True, "result": result.output}
        return {"ok": False, "error": result.error}


class GeminiLiveModelSession(ModelSession):
    """Adapt one Gemini connection to the common turn-based model session."""

    def __init__(self, session: genai.live.AsyncSession) -> None:
        """Initialize isolated provider state for one application execution."""
        self._session = session
        self._started = False
        self._pending_calls: dict[str, str] = {}
        self._messages: AsyncIterator[types.LiveServerMessage] | None = None
        self._answer_parts: list[str] = []

    async def send_message(self, message: str) -> ModelReply:
        """Send one initial text turn and collect its next model boundary."""
        self._start()
        await self._session.send_realtime_input(text=message)
        return await self._collect_reply()

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        """Return a complete result batch and collect the next model boundary."""
        await self._send_tool_results(results)
        return await self._collect_reply()

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Send one initial text turn and stream audio plus its transcription."""
        self._start()
        return self._send_message_and_stream(message)

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Return tool results and continue the same native-audio response."""
        return self._send_results_and_stream(results)

    def _start(self) -> None:
        """Reject reuse of a session for more than one initial user message."""
        if self._started:
            raise GeminiLiveSessionStateError("The initial model message has already been sent")
        self._started = True

    async def _send_message_and_stream(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Defer asynchronous text transmission until stream iteration starts."""
        await self._session.send_realtime_input(text=message)
        async for event in self._stream_until_boundary():
            yield event

    async def _send_results_and_stream(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Defer asynchronous tool continuation until stream iteration starts."""
        await self._send_tool_results(results)
        async for event in self._stream_until_boundary():
            yield event

    async def _collect_reply(self) -> ModelReply:
        """Consume audio events while retaining the normalized terminal reply."""
        reply: ModelReply | None = None
        async for event in self._stream_until_boundary():
            if isinstance(event, ModelStreamCompleted):
                reply = event.reply
        if reply is None:
            raise GeminiLiveProtocolError("Gemini Live ended without a terminal reply")
        return reply

    async def _send_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Validate result IDs and resume the paused Gemini generation."""
        if not self._started:
            raise GeminiLiveSessionStateError("Cannot send tool results before the initial message")
        result_ids = [result.call_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise GeminiLiveSessionStateError("Tool result call IDs must be unique")
        if set(result_ids) != set(self._pending_calls):
            raise GeminiLiveSessionStateError(
                "Tool results must match all pending Gemini Live calls"
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

    async def _stream_until_boundary(self) -> AsyncIterator[ModelStreamEvent]:
        """Yield media until a tool request or completed turn pauses orchestration."""
        while True:
            message = await self._next_message()
            server_content = message.server_content
            if server_content is not None:
                model_turn = server_content.model_turn
                if model_turn is not None:
                    for part in model_turn.parts or []:
                        inline_data = part.inline_data
                        if inline_data is None or inline_data.data is None:
                            continue
                        yield ModelAudioDelta(
                            data=inline_data.data,
                            mime_type=inline_data.mime_type or "audio/pcm;rate=24000",
                        )
                transcription = server_content.output_transcription
                if transcription is not None and transcription.text:
                    self._answer_parts.append(transcription.text)
                    yield ModelTextDelta(text=transcription.text)
                if server_content.interrupted:
                    yield ModelAudioInterrupted()

            tool_call = message.tool_call
            if tool_call is not None:
                calls = tuple(self._normalize_call(call) for call in tool_call.function_calls or [])
                if calls:
                    if self._pending_calls:
                        raise GeminiLiveSessionStateError(
                            "Gemini emitted new tool calls before prior results were returned"
                        )
                    self._pending_calls = {call.call_id: call.tool_name for call in calls}
                    yield ModelStreamCompleted(reply=self._reply(calls))
                    return

            if server_content is not None and server_content.turn_complete:
                yield ModelStreamCompleted(reply=self._reply(()))
                return

    async def _next_message(self) -> types.LiveServerMessage:
        """Preserve an SDK receive iterator across synchronous tool boundaries."""
        while True:
            if self._messages is None:
                self._messages = self._session.receive()
            try:
                return await anext(self._messages)
            except StopAsyncIteration:
                self._messages = None

    def _reply(self, calls: tuple[ToolCall, ...]) -> ModelReply:
        """Build a provider-neutral reply with the accumulated spoken transcript."""
        return ModelReply(
            response_id=str(uuid4()),
            text="".join(self._answer_parts).strip(),
            tool_calls=calls,
        )

    @staticmethod
    def _normalize_call(call: types.FunctionCall) -> ToolCall:
        """Validate one complete Gemini function call and its JSON arguments."""
        if not call.id or not call.name:
            raise GeminiLiveProtocolError("Gemini emitted a tool call without ID or name")
        arguments = call.args or {}
        if not isinstance(arguments, dict):
            raise GeminiLiveProtocolError(f"Tool {call.name} arguments must be a JSON object")
        return ToolCall(call_id=call.id, tool_name=call.name, arguments=arguments)
