import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from openai import AsyncOpenAI
from openai.types.responses import Response

from application.ports import ModelGateway, ModelSession
from domain.agent import AgentDefinition
from domain.conversations import ConversationItem, ConversationMessage
from domain.events import (
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from domain.model import ModelReply
from domain.tools import ToolCall, ToolResult, ToolSpec
from domain.types import JsonObject

logger = structlog.get_logger(__name__)


class ModelProtocolError(RuntimeError):
    """Raised when a provider returns an invalid model interaction."""


class SessionStateError(RuntimeError):
    """Raised when a model session is used out of order."""


class OpenAIResponsesGateway(ModelGateway):
    """Factory for isolated Responses API sessions sharing one HTTP client."""

    def __init__(self, client: AsyncOpenAI) -> None:
        """Store the shared asynchronous client without conversation state."""
        self._client = client

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncIterator[ModelSession]:
        """Yield a request-scoped session that translates neutral contracts."""
        yield OpenAIModelSession(self._client, definition, tools, history)


class OpenAIModelSession(ModelSession):
    """Request-scoped adapter that owns all OpenAI-specific conversation state."""

    def __init__(
        self,
        client: AsyncOpenAI,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> None:
        """Initialize isolated Responses API state over a shared HTTP client."""
        self._client = client
        self._definition = definition
        self._tool_schemas = [self._to_openai_tool(tool) for tool in tools]
        self._input_items: list[dict[str, Any]] = [
            self._to_openai_history_item(item) for item in history
        ]
        self._started = False
        self._pending_call_ids: frozenset[str] = frozenset()

    async def send_message(self, message: str) -> ModelReply:
        """Send the initial user message exactly once for this session."""
        self._start(message)
        return await self._request_model()

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        """Translate a complete neutral result batch and continue the response."""
        self._append_tool_results(results)
        return await self._request_model()

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Start a streaming turn and normalize OpenAI events for the application."""
        self._start(message)
        return self._stream_model()

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Continue streaming after translating all pending neutral tool results."""
        self._append_tool_results(results)
        return self._stream_model()

    def _start(self, message: str) -> None:
        """Initialize session state with exactly one user message."""
        if self._started:
            raise SessionStateError("The initial model message has already been sent")
        self._started = True
        self._input_items.append({"role": "user", "content": message})

    def _append_tool_results(self, results: tuple[ToolResult, ...]) -> None:
        """Validate and append provider-specific outputs for pending tool calls."""
        if not self._started:
            raise SessionStateError("Cannot send tool results before the initial message")
        if not self._pending_call_ids:
            raise SessionStateError("The model has no pending tool calls")

        result_ids = [result.call_id for result in results]
        if len(set(result_ids)) != len(result_ids):
            raise SessionStateError("Tool result call IDs must be unique")
        if set(result_ids) != self._pending_call_ids:
            raise SessionStateError("Tool results must match all pending model tool calls")

        for result in results:
            self._input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": result.call_id,
                    "output": json.dumps(
                        self._tool_result_payload(result), ensure_ascii=False, default=str
                    ),
                }
            )

        self._pending_call_ids = frozenset()

    @classmethod
    def _to_openai_history_item(cls, item: ConversationItem) -> dict[str, Any]:
        """Translate one retained neutral history item to Responses API input."""
        if isinstance(item, ConversationMessage):
            return {"role": item.role, "content": item.content}
        if isinstance(item, ToolCall):
            return {
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.tool_name,
                "arguments": json.dumps(item.arguments, ensure_ascii=False, default=str),
            }
        return {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": json.dumps(cls._tool_result_payload(item), ensure_ascii=False, default=str),
        }

    @staticmethod
    def _tool_result_payload(result: ToolResult) -> JsonObject:
        """Build the provider payload used for live and retained tool results."""
        if result.error is None:
            return {"ok": True, "result": result.output}
        return {"ok": False, "error": result.error}

    async def _request_model(self) -> ModelReply:
        """Call Responses API and normalize returned text and function calls."""
        logger.info(
            "openai_request_started",
            model=self._definition.model,
            input_item_count=len(self._input_items),
        )
        response = await self._client.responses.create(
            model=self._definition.model,
            instructions=self._definition.instructions,
            input=list(self._input_items),  # type: ignore[arg-type]
            tools=self._tool_schemas,  # type: ignore[arg-type]
            parallel_tool_calls=True,
        )
        reply = self._normalize_response(response)
        logger.info(
            "openai_request_completed",
            model=self._definition.model,
            response_id=reply.response_id,
            request_id=getattr(response, "_request_id", None),
            tool_call_count=len(reply.tool_calls),
        )
        return reply

    async def _stream_model(self) -> AsyncIterator[ModelStreamEvent]:
        """Stream one Responses API turn and emit one terminal normalized reply."""
        logger.info(
            "openai_stream_started",
            model=self._definition.model,
            input_item_count=len(self._input_items),
        )
        async with self._client.responses.stream(
            model=self._definition.model,
            instructions=self._definition.instructions,
            input=list(self._input_items),  # type: ignore[arg-type]
            tools=self._tool_schemas,  # type: ignore[arg-type]
            parallel_tool_calls=True,
        ) as stream:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    yield ModelTextDelta(text=event.delta)
            response = await stream.get_final_response()

        reply = self._normalize_response(response)
        logger.info(
            "openai_stream_completed",
            model=self._definition.model,
            response_id=reply.response_id,
            tool_call_count=len(reply.tool_calls),
        )
        yield ModelStreamCompleted(reply=reply)

    def _normalize_response(self, response: Response) -> ModelReply:
        """Persist provider state and translate a complete OpenAI response."""
        calls: list[ToolCall] = []
        for item in response.output:
            self._input_items.append(
                item.model_dump(exclude_none=True, exclude={"parsed_arguments"})
            )
            if item.type != "function_call":
                continue
            calls.append(
                ToolCall(
                    call_id=item.call_id,
                    tool_name=item.name,
                    arguments=self._parse_arguments(item.arguments, item.name),
                )
            )

        self._pending_call_ids = frozenset(call.call_id for call in calls)
        return ModelReply(
            response_id=response.id,
            text=response.output_text,
            tool_calls=tuple(calls),
        )

    @staticmethod
    def _to_openai_tool(tool: ToolSpec) -> JsonObject:
        """Translate a neutral tool specification into OpenAI function format."""
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.arguments_schema,
            "strict": True,
        }

    @staticmethod
    def _parse_arguments(arguments_json: str, tool_name: str) -> JsonObject:
        """Decode and validate JSON arguments emitted by an OpenAI function call."""
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(f"Tool {tool_name} returned invalid JSON arguments") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError(f"Tool {tool_name} arguments must be a JSON object")
        return arguments
