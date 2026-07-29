import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

import structlog
from openai import AsyncOpenAI

from application.ports import RealtimeModelGateway, RealtimeModelSession
from domain.agent import AgentDefinition
from domain.conversations import ConversationItem, ConversationMessage
from domain.costs import ModelUsage
from domain.realtime import (
    AudioChunk,
    RealtimeModelActivityEnded,
    RealtimeModelActivityStarted,
    RealtimeModelAudioDelta,
    RealtimeModelAudioInterrupted,
    RealtimeModelEvent,
    RealtimeModelInputTranscriptDelta,
    RealtimeModelOutputTranscriptDelta,
    RealtimeModelToolCall,
    RealtimeModelTurnCompleted,
    RealtimeSessionCapabilities,
    RealtimeSessionOptions,
)
from domain.tools import ToolCall, ToolResult, ToolSpec

logger = structlog.get_logger(__name__)

OPENAI_REALTIME_SAMPLE_RATE = 24_000
OPENAI_REALTIME_MIME_TYPE = f"audio/pcm;rate={OPENAI_REALTIME_SAMPLE_RATE}"


class OpenAIRealtimeProtocolError(RuntimeError):
    """Signal an invalid or failed event from the OpenAI Realtime API."""


class OpenAIRealtimeSessionStateError(RuntimeError):
    """Reject tool results or media operations that violate session ordering."""


class _OpenAIRealtimeConnection(Protocol):
    """Small SDK surface used by the adapter and its deterministic tests."""

    async def send(self, event: dict[str, Any]) -> None:
        """Send one typed Realtime client event."""

    def __aiter__(self) -> AsyncIterator[Any]:
        """Yield Realtime server events until the connection closes."""


class OpenAIRealtimeGateway(RealtimeModelGateway):
    """Open speech-to-speech sessions through the official OpenAI Python SDK."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        voice_name: str,
        transcription_model: str,
        input_language_code: str | None,
        reasoning_effort: str | None,
    ) -> None:
        """Store provider configuration while keeping session state connection-local."""
        self._client = client
        self._voice_name = voice_name
        self._transcription_model = transcription_model
        self._input_language_code = input_language_code
        self._reasoning_effort = reasoning_effort

    @property
    def capabilities(self) -> RealtimeSessionCapabilities:
        """Describe the stable subset implemented by this OpenAI adapter."""
        return RealtimeSessionCapabilities(
            input_audio_mime_type=OPENAI_REALTIME_MIME_TYPE,
            output_audio_mime_type=OPENAI_REALTIME_MIME_TYPE,
            activity_detection_modes=("automatic", "explicit"),
            supports_barge_in=True,
            recovery_mode="none",
        )

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
        options: RealtimeSessionOptions,
    ) -> AsyncGenerator[RealtimeModelSession, None]:
        """Connect, configure, and prefill one isolated Realtime conversation."""
        logger.info(
            "openai_realtime_session_connecting",
            model=definition.model,
            tool_count=len(tools),
            history_item_count=len(history),
        )
        manager = self._client.realtime.connect(model=definition.model, max_retries=0)
        async with manager as connection:
            session = OpenAIRealtimeModelSession(
                cast(_OpenAIRealtimeConnection, connection),
                activity_detection=options.activity.detection,
                silence_duration_ms=options.activity.silence_duration_ms,
            )
            await session.configure(
                definition,
                tools,
                history,
                options,
                voice_name=self._voice_name,
                transcription_model=self._transcription_model,
                input_language_code=self._input_language_code,
                reasoning_effort=self._reasoning_effort,
            )
            logger.info("openai_realtime_session_connected", model=definition.model)
            yield session


class OpenAIRealtimeModelSession(RealtimeModelSession):
    """Translate OpenAI Realtime client/server events into neutral session events."""

    def __init__(
        self,
        connection: _OpenAIRealtimeConnection,
        *,
        activity_detection: str = "automatic",
        silence_duration_ms: int | None = None,
    ) -> None:
        """Initialize one provider connection and its response correlation state."""
        self._connection = connection
        self._activity_detection = activity_detection
        self._silence_duration_ms = silence_duration_ms or 500
        self._pending_calls: dict[str, str] = {}
        self._response_calls: dict[str, dict[str, ToolCall]] = {}
        self._pending_transcriptions: set[str] = set()
        self._transcript_parts: dict[str, list[str]] = {}
        self._pending_completion: tuple[str, ModelUsage] | None = None
        self._response_active = False
        self._audio_sent_since_boundary = False

    async def configure(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
        options: RealtimeSessionOptions,
        *,
        voice_name: str,
        transcription_model: str,
        input_language_code: str | None,
        reasoning_effort: str | None,
    ) -> None:
        """Apply session policy and replay retained neutral history as text items."""
        session: dict[str, Any] = {
            "type": "realtime",
            "model": definition.model,
            "instructions": definition.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_REALTIME_SAMPLE_RATE},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": {
                        "model": transcription_model,
                        **({"language": input_language_code} if input_language_code else {}),
                    },
                    "turn_detection": self._turn_detection(options),
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_REALTIME_SAMPLE_RATE},
                    "voice": voice_name,
                },
            },
            "tools": [self._tool_payload(tool) for tool in tools],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if reasoning_effort is not None:
            session["reasoning"] = {"effort": reasoning_effort}
        await self._send({"type": "session.update", "session": session})
        for item in self._history_payloads(history):
            await self._send({"type": "conversation.item.create", "item": item})

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Append one base64 PCM16 fragment without committing the provider buffer."""
        if chunk.mime_type != OPENAI_REALTIME_MIME_TYPE:
            raise OpenAIRealtimeSessionStateError(
                f"OpenAI Realtime requires {OPENAI_REALTIME_MIME_TYPE}, got {chunk.mime_type}"
            )
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk.data).decode("ascii"),
            }
        )
        self._audio_sent_since_boundary = True

    async def end_audio(self) -> None:
        """Give server VAD enough trailing silence to close a stopped microphone."""
        if self._activity_detection != "automatic" or not self._audio_sent_since_boundary:
            return
        silence_ms = self._silence_duration_ms + 100
        silence_bytes = b"\x00\x00" * (OPENAI_REALTIME_SAMPLE_RATE * silence_ms // 1000)
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(silence_bytes).decode("ascii"),
            }
        )
        self._audio_sent_since_boundary = False

    async def start_activity(self) -> None:
        """Clear any stale manual-VAD buffer before a new explicit utterance."""
        if self._activity_detection == "explicit":
            await self._send({"type": "input_audio_buffer.clear"})

    async def end_activity(self) -> None:
        """Commit explicit-VAD audio and request its model response."""
        if self._activity_detection != "explicit":
            return
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})
        self._audio_sent_since_boundary = False

    async def send_text(self, text: str) -> None:
        """Append a user text item and explicitly request an audio response."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Return every pending function output and continue the same logical turn."""
        result_ids = [result.call_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise OpenAIRealtimeSessionStateError("Tool result call IDs must be unique")
        if set(result_ids) != set(self._pending_calls):
            raise OpenAIRealtimeSessionStateError(
                "Tool results must match all pending OpenAI Realtime calls"
            )
        for result in results:
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": self._tool_result_text(result),
                    },
                }
            )
        self._pending_calls = {}
        await self._send({"type": "response.create"})

    async def receive(self) -> AsyncIterator[RealtimeModelEvent]:
        """Normalize streaming media, transcripts, VAD, tools, and response terminals."""
        async for event in self._connection:
            event_type = str(getattr(event, "type", ""))
            if event_type == "input_audio_buffer.speech_started":
                if self._response_active:
                    yield RealtimeModelAudioInterrupted()
                yield RealtimeModelActivityStarted()
            elif event_type == "input_audio_buffer.speech_stopped":
                yield RealtimeModelActivityEnded()
            elif event_type == "input_audio_buffer.committed":
                item_id = self._required_text(event, "item_id")
                self._pending_transcriptions.add(item_id)
                self._transcript_parts.setdefault(item_id, [])
                self._audio_sent_since_boundary = False
            elif event_type == "conversation.item.input_audio_transcription.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    item_id = self._required_text(event, "item_id")
                    self._transcript_parts.setdefault(item_id, []).append(delta)
                    yield RealtimeModelInputTranscriptDelta(text=delta)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                async for normalized in self._complete_transcription(event):
                    yield normalized
            elif event_type == "conversation.item.input_audio_transcription.failed":
                raise OpenAIRealtimeProtocolError("OpenAI input audio transcription failed")
            elif event_type == "response.created":
                self._response_active = True
            elif event_type == "response.output_audio.delta":
                delta = self._required_text(event, "delta")
                try:
                    audio = base64.b64decode(delta, validate=True)
                except ValueError as exc:
                    raise OpenAIRealtimeProtocolError(
                        "OpenAI emitted invalid base64 audio"
                    ) from exc
                yield RealtimeModelAudioDelta(data=audio, mime_type=OPENAI_REALTIME_MIME_TYPE)
            elif event_type in {
                "response.output_audio_transcript.delta",
                "response.output_text.delta",
            }:
                delta = self._required_text(event, "delta")
                yield RealtimeModelOutputTranscriptDelta(text=delta)
            elif event_type == "response.output_item.done":
                self._capture_tool_call(event)
            elif event_type == "response.done":
                async for normalized in self._complete_response(event):
                    yield normalized
            elif event_type == "error":
                error = getattr(event, "error", None)
                code = getattr(error, "code", None) or "realtime_error"
                raise OpenAIRealtimeProtocolError(f"OpenAI Realtime error: {code}")

    async def _complete_transcription(
        self,
        event: Any,
    ) -> AsyncIterator[RealtimeModelEvent]:
        """Emit any missing transcript suffix before releasing a gated terminal."""
        item_id = self._required_text(event, "item_id")
        transcript = self._required_text(event, "transcript")
        emitted = "".join(self._transcript_parts.pop(item_id, []))
        if transcript.startswith(emitted):
            suffix = transcript[len(emitted) :]
            if suffix:
                yield RealtimeModelInputTranscriptDelta(text=suffix)
        elif transcript and not emitted:
            yield RealtimeModelInputTranscriptDelta(text=transcript)
        self._pending_transcriptions.discard(item_id)
        if self._pending_completion is not None and not self._pending_transcriptions:
            response_id, usage = self._pending_completion
            self._pending_completion = None
            yield RealtimeModelTurnCompleted(response_id=response_id, usage=usage)

    async def _complete_response(self, event: Any) -> AsyncIterator[RealtimeModelEvent]:
        """Distinguish tool boundaries from successful logical-turn completion."""
        response = getattr(event, "response", None)
        if response is None:
            raise OpenAIRealtimeProtocolError("OpenAI response.done omitted its response")
        response_id = getattr(response, "id", None)
        if not isinstance(response_id, str) or not response_id:
            raise OpenAIRealtimeProtocolError("OpenAI response.done omitted its response ID")
        self._response_active = False
        status = getattr(response, "status", None)
        if status != "completed":
            return
        usage = self._normalize_usage(getattr(response, "usage", None))
        self._capture_response_calls(response_id, getattr(response, "output", None) or [])
        calls = tuple(self._response_calls.pop(response_id, {}).values())
        if calls:
            if self._pending_calls:
                raise OpenAIRealtimeSessionStateError(
                    "OpenAI emitted new calls before prior tool results returned"
                )
            self._pending_calls = {call.call_id: call.tool_name for call in calls}
            yield RealtimeModelToolCall(calls=calls, usage=usage)
            return
        if self._pending_transcriptions:
            self._pending_completion = (response_id, usage)
            return
        yield RealtimeModelTurnCompleted(response_id=response_id, usage=usage)

    @staticmethod
    def _normalize_usage(usage: object | None) -> ModelUsage:
        """Translate Realtime text/audio counters into shared usage fields."""
        if usage is None:
            return ModelUsage()
        input_details = getattr(usage, "input_token_details", None)
        output_details = getattr(usage, "output_token_details", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cached_tokens = int(getattr(input_details, "cached_tokens", 0) or 0)
        audio_tokens = int(getattr(input_details, "audio_tokens", 0) or 0)
        cached_details = getattr(input_details, "cached_tokens_details", None)
        cached_audio = int(getattr(cached_details, "audio_tokens", 0) or 0)
        # Older SDK types expose only the cached total. Infer the minimum overlap
        # needed to keep input categories consistent until details are available.
        cached_audio = max(cached_audio, cached_tokens + audio_tokens - input_tokens)
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cached_input_tokens=cached_tokens,
            cached_input_audio_tokens=cached_audio,
            input_audio_tokens=audio_tokens,
            output_audio_tokens=int(getattr(output_details, "audio_tokens", 0) or 0),
        )

    def _capture_tool_call(self, event: Any) -> None:
        """Retain one completed function item until its response boundary."""
        response_id = self._required_text(event, "response_id")
        item = getattr(event, "item", None)
        call = self._normalize_call(item)
        if call is not None:
            self._response_calls.setdefault(response_id, {})[call.call_id] = call

    def _capture_response_calls(self, response_id: str, items: list[Any]) -> None:
        """Use response.done as a complete fallback for missed item events."""
        for item in items:
            call = self._normalize_call(item)
            if call is not None:
                self._response_calls.setdefault(response_id, {})[call.call_id] = call

    @staticmethod
    def _normalize_call(item: Any) -> ToolCall | None:
        """Validate one complete OpenAI function-call item."""
        if getattr(item, "type", None) != "function_call":
            return None
        call_id = getattr(item, "call_id", None)
        name = getattr(item, "name", None)
        raw_arguments = getattr(item, "arguments", None)
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise OpenAIRealtimeProtocolError("OpenAI emitted a function call without ID or name")
        if not isinstance(raw_arguments, str):
            raise OpenAIRealtimeProtocolError(f"Tool {name} omitted JSON arguments")
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise OpenAIRealtimeProtocolError(f"Tool {name} emitted invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise OpenAIRealtimeProtocolError(f"Tool {name} arguments must be an object")
        return ToolCall(call_id=call_id, tool_name=name, arguments=arguments)

    async def _send(self, event: dict[str, Any]) -> None:
        """Send one event through the official SDK connection."""
        await self._connection.send(event)

    @staticmethod
    def _tool_payload(tool: ToolSpec) -> dict[str, Any]:
        """Translate one neutral function declaration."""
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.arguments_schema,
        }

    @classmethod
    def _history_payloads(cls, history: tuple[ConversationItem, ...]) -> list[dict[str, Any]]:
        """Translate retained text and tool exchanges into Realtime conversation items."""
        payloads: list[dict[str, Any]] = []
        for item in history:
            if isinstance(item, ConversationMessage):
                content_type = "output_text" if item.role == "assistant" else "input_text"
                payloads.append(
                    {
                        "type": "message",
                        "role": item.role,
                        "content": [{"type": content_type, "text": item.content}],
                    }
                )
            elif isinstance(item, ToolCall):
                payloads.append(
                    {
                        "type": "function_call",
                        "call_id": item.call_id,
                        "name": item.tool_name,
                        "arguments": json.dumps(item.arguments, ensure_ascii=False),
                    }
                )
            else:
                payloads.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": cls._tool_result_text(item),
                    }
                )
        return payloads

    @staticmethod
    def _tool_result_text(result: ToolResult) -> str:
        """Encode one neutral result as the function output's JSON text."""
        payload = (
            {"ok": True, "result": result.output}
            if result.error is None
            else {"ok": False, "error": result.error}
        )
        return json.dumps(payload, ensure_ascii=False, default=str)

    @staticmethod
    def _required_text(event: Any, field: str) -> str:
        """Read one required non-empty string from a provider event."""
        value = getattr(event, field, None)
        if not isinstance(value, str) or not value:
            raise OpenAIRealtimeProtocolError(
                f"OpenAI event {getattr(event, 'type', 'unknown')} omitted {field}"
            )
        return value

    @staticmethod
    def _turn_detection(options: RealtimeSessionOptions) -> dict[str, Any] | None:
        """Map neutral automatic/explicit activity policy to OpenAI server VAD."""
        activity = options.activity
        if activity.detection == "explicit":
            return None
        threshold = None
        if activity.start_sensitivity == "high":
            threshold = 0.35
        elif activity.start_sensitivity == "low":
            threshold = 0.65
        return {
            "type": "server_vad",
            "create_response": True,
            "interrupt_response": activity.interrupt_on_activity,
            **({"threshold": threshold} if threshold is not None else {}),
            **(
                {"prefix_padding_ms": activity.prefix_padding_ms}
                if activity.prefix_padding_ms is not None
                else {}
            ),
            **(
                {"silence_duration_ms": activity.silence_duration_ms}
                if activity.silence_duration_ms is not None
                else {}
            ),
        }
