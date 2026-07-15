import asyncio
import time
from collections.abc import AsyncIterator

import structlog

from application.ports import ConversationRepository, ModelGateway
from application.tools import ToolRegistry
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.events import (
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
    ModelStreamCompleted,
    ModelTextDelta,
)
from domain.tools import (
    ToolCall,
    ToolCallRecord,
    ToolResult,
)

logger = structlog.get_logger(__name__)


class ToolRoundsExceededError(RuntimeError):
    """Raised when a model keeps requesting tools beyond the configured limit."""

    pass


class IncompleteModelStreamError(RuntimeError):
    """Raised when a provider stream ends without a terminal accumulated reply."""

    pass


class AgentService:
    """Provider-neutral orchestration of model and tool interactions."""

    def __init__(
        self,
        model_gateway: ModelGateway,
        tools: ToolRegistry,
        conversations: ConversationRepository,
        *,
        max_tool_rounds: int = 8,
    ) -> None:
        """Initialize the orchestrator with shared gateways and the tool catalog."""
        self._model_gateway = model_gateway
        self._tools = tools
        self._conversations = conversations
        self._max_tool_rounds = max_tool_rounds

    async def run(
        self,
        message: str,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
    ) -> AgentResult:
        """Continue and persist one owned conversation after a complete model run."""
        conversation = await self._load_conversation(conversation_key)
        selected_tools = self._tools.select(definition.tool_names)
        session = self._model_gateway.create_session(
            definition, selected_tools.specs, conversation.messages
        )
        records: list[ToolCallRecord] = []
        turn_items: list[ConversationItem] = [ConversationMessage(role="user", content=message)]

        logger.info(
            "agent_run_started",
            model=definition.model,
            message_length=len(message),
            tool_count=len(selected_tools.specs),
        )
        reply = await session.send_message(message)

        for round_number in range(self._max_tool_rounds):
            logger.info(
                "model_reply_received",
                response_id=reply.response_id,
                tool_call_count=len(reply.tool_calls),
                round=round_number,
            )
            if not reply.tool_calls:
                break

            round_records, results = await self._execute_tools(reply.tool_calls, selected_tools)
            records.extend(round_records)
            turn_items.extend(reply.tool_calls)
            turn_items.extend(results)
            reply = await session.send_tool_results(results)
        else:
            if reply.tool_calls:
                raise ToolRoundsExceededError(f"Agent exceeded {self._max_tool_rounds} tool rounds")

        logger.info(
            "agent_run_completed",
            response_id=reply.response_id,
            tool_call_count=len(records),
        )
        result = AgentResult(
            answer=reply.text,
            response_id=reply.response_id,
            conversation_id=conversation_key.conversation_id,
            tool_calls=tuple(records),
        )
        turn_items.append(ConversationMessage(role="assistant", content=result.answer))
        await self._persist_turn(conversation, tuple(turn_items))
        return result

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream one conversation turn and persist it before terminal success."""
        conversation = await self._load_conversation(conversation_key)
        selected_tools = self._tools.select(definition.tool_names)
        session = self._model_gateway.create_session(
            definition, selected_tools.specs, conversation.messages
        )
        model_events = session.stream_message(message)
        records: list[ToolCallRecord] = []
        turn_items: list[ConversationItem] = [ConversationMessage(role="user", content=message)]

        logger.info(
            "agent_stream_started",
            model=definition.model,
            message_length=len(message),
            tool_count=len(selected_tools.specs),
        )

        for round_number in range(self._max_tool_rounds + 1):
            completed_reply = None
            async for event in model_events:
                if isinstance(event, ModelTextDelta):
                    yield AgentTextDelta(text=event.text)
                elif isinstance(event, ModelStreamCompleted):
                    if completed_reply is not None:
                        raise IncompleteModelStreamError(
                            "Model stream emitted more than one terminal event"
                        )
                    completed_reply = event.reply

            if completed_reply is None:
                raise IncompleteModelStreamError("Model stream ended without a terminal response")

            logger.info(
                "model_stream_completed",
                response_id=completed_reply.response_id,
                tool_call_count=len(completed_reply.tool_calls),
                round=round_number,
            )
            if not completed_reply.tool_calls:
                result = AgentResult(
                    answer=completed_reply.text,
                    response_id=completed_reply.response_id,
                    conversation_id=conversation_key.conversation_id,
                    tool_calls=tuple(records),
                )
                turn_items.append(ConversationMessage(role="assistant", content=result.answer))
                await self._persist_turn(conversation, tuple(turn_items))
                logger.info(
                    "agent_stream_completed",
                    response_id=result.response_id,
                    tool_call_count=len(records),
                )
                yield AgentStreamCompleted(result=result)
                return

            if round_number == self._max_tool_rounds:
                raise ToolRoundsExceededError(f"Agent exceeded {self._max_tool_rounds} tool rounds")

            for call in completed_reply.tool_calls:
                yield AgentToolStarted(call_id=call.call_id, tool_name=call.tool_name)
            round_records, results = await self._execute_tools(
                completed_reply.tool_calls, selected_tools
            )
            records.extend(round_records)
            turn_items.extend(completed_reply.tool_calls)
            turn_items.extend(results)
            for record in round_records:
                yield AgentToolCompleted(record=record)
            model_events = session.stream_tool_results(results)

        raise AssertionError("Unreachable")

    async def delete_conversation(self, key: ConversationKey) -> bool:
        """Delete a conversation after enforcing repository ownership checks."""
        return await self._conversations.delete(key)

    async def _load_conversation(self, key: ConversationKey) -> Conversation:
        """Load an existing conversation or create a new version-zero aggregate."""
        return await self._conversations.load(key) or Conversation(key=key)

    async def _persist_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> None:
        """Append one complete model/tool turn and save it with optimistic concurrency."""
        await self._conversations.save_turn(conversation, turn)

    async def _execute_tools(
        self,
        calls: tuple[ToolCall, ...],
        tools: ToolRegistry,
    ) -> tuple[tuple[ToolCallRecord, ...], tuple[ToolResult, ...]]:
        """Execute a batch of independent tool calls concurrently."""
        executions = await asyncio.gather(*(self._execute_tool_call(call, tools) for call in calls))
        records, results = zip(*executions, strict=True)
        return tuple(records), tuple(results)

    async def _execute_tool_call(
        self,
        call: ToolCall,
        tools: ToolRegistry,
    ) -> tuple[ToolCallRecord, ToolResult]:
        """Execute and measure one tool call, converting failures into results."""
        started = time.perf_counter()
        logger.info("tool_call_started", call_id=call.call_id, tool_name=call.tool_name)

        try:
            output = await tools.execute(call.tool_name, call.arguments)
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
