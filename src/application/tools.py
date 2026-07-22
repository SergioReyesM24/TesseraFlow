import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

import structlog
from pydantic import BaseModel, ConfigDict

from domain.conversations import ConversationKey
from domain.interactions import InteractionDeliveryMode
from domain.tools import ToolCall, ToolCallRecord, ToolResult, ToolSpec
from domain.types import JsonObject

logger = structlog.get_logger(__name__)


class ToolArguments(BaseModel):
    """Base model that produces closed JSON schemas for strict function calling."""

    model_config = ConfigDict(extra="forbid")


ArgumentsT = TypeVar("ArgumentsT", bound=ToolArguments)


class ToolExecutionContext(BaseModel):
    """Explicit multiuser context supplied to tools for one isolated execution."""

    conversation_id: str
    user_id: str
    delivery_mode: InteractionDeliveryMode = "turn_based"

    @classmethod
    def from_conversation(
        cls,
        key: ConversationKey,
        *,
        delivery_mode: InteractionDeliveryMode = "turn_based",
    ) -> "ToolExecutionContext":
        """Build a validated tool context from the owned conversation key."""
        return cls(
            conversation_id=key.conversation_id,
            user_id=key.user_id,
            delivery_mode=delivery_mode,
        )

    def conversation_key(self) -> ConversationKey:
        """Recover the neutral ownership key needed by application services."""
        return ConversationKey(
            conversation_id=self.conversation_id,
            user_id=self.user_id,
        )


class AgentTool(ABC, Generic[ArgumentsT]):
    """Small interface implemented by every local function tool."""

    name: ClassVar[str]
    description: ClassVar[str]
    arguments_model: ClassVar[type[ArgumentsT]]

    @abstractmethod
    async def execute(self, arguments: ArgumentsT, context: ToolExecutionContext) -> Any:
        """Execute validated arguments and return a JSON-serializable value."""

    def spec(self) -> ToolSpec:
        """Build the provider-neutral tool declaration exposed to a model."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            arguments_schema=self.arguments_model.model_json_schema(),
        )

    async def invoke(self, raw_arguments: JsonObject, context: ToolExecutionContext) -> Any:
        """Validate untrusted model arguments before invoking the implementation."""
        arguments = self.arguments_model.model_validate(raw_arguments)
        return await self.execute(arguments, context)


class ToolNotFoundError(LookupError):
    """Raised when an agent references a tool absent from its allowed catalog."""

    pass


class ToolRegistry:
    """Immutable-by-convention catalog for selecting and executing local tools."""

    def __init__(self, tools: list[AgentTool[Any]]) -> None:
        """Index tools by their unique public names."""
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool names must be unique")

    @property
    def names(self) -> tuple[str, ...]:
        """Return tool names in deterministic registration order."""
        return tuple(self._tools)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """Return provider-neutral specifications for all selected tools."""
        return tuple(tool.spec() for tool in self._tools.values())

    def select(self, names: tuple[str, ...]) -> "ToolRegistry":
        """Create a restricted registry containing exactly the requested tools."""
        if len(set(names)) != len(names):
            raise ValueError("Agent tool names must be unique")
        try:
            return ToolRegistry([self._tools[name] for name in names])
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool in agent definition: {exc.args[0]}") from exc

    async def execute(
        self,
        name: str,
        arguments: JsonObject,
        context: ToolExecutionContext,
    ) -> Any:
        """Execute a named tool after validating its provider-neutral arguments."""
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool: {name}") from exc
        return await tool.invoke(arguments, context)


class ToolExecutor:
    """Execute validated model tool calls and produce neutral audit records."""

    async def execute(
        self,
        calls: tuple[ToolCall, ...],
        tools: ToolRegistry,
        context: ToolExecutionContext,
    ) -> tuple[tuple[ToolCallRecord, ...], tuple[ToolResult, ...]]:
        """Execute one complete batch concurrently and preserve call ordering."""
        executions = await asyncio.gather(
            *(self._execute_call(call, tools, context) for call in calls)
        )
        records, results = zip(*executions, strict=True)
        return tuple(records), tuple(results)

    async def _execute_call(
        self,
        call: ToolCall,
        tools: ToolRegistry,
        context: ToolExecutionContext,
    ) -> tuple[ToolCallRecord, ToolResult]:
        """Measure one invocation and convert expected failures into model results."""
        started = time.perf_counter()
        logger.info("tool_call_started", call_id=call.call_id, tool_name=call.tool_name)
        try:
            output = await tools.execute(call.tool_name, call.arguments, context)
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "tool_call_completed",
                call_id=call.call_id,
                tool_name=call.tool_name,
                duration_ms=round(duration_ms, 2),
            )
            return (
                ToolCallRecord(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    status="success",
                    output=output,
                    error=None,
                    duration_ms=duration_ms,
                ),
                ToolResult(call_id=call.call_id, output=output),
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            error_message = str(exc) or type(exc).__name__
            logger.warning(
                "tool_call_failed",
                call_id=call.call_id,
                tool_name=call.tool_name,
                error_type=type(exc).__name__,
                duration_ms=round(duration_ms, 2),
            )
            return (
                ToolCallRecord(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    status="error",
                    output=None,
                    error=error_message,
                    duration_ms=duration_ms,
                ),
                ToolResult(call_id=call.call_id, error=error_message),
            )
