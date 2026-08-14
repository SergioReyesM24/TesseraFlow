from collections.abc import AsyncGenerator
from uuid import uuid4

import structlog

from application.conversations import ConversationNotFoundError
from application.evaluations import (
    ToolCallEvaluationGate,
    ToolCallGateDecision,
    append_evaluation_trace,
    tool_call_revision_limit_results,
)
from application.ports import ConversationRepository, EvaluationTraceRepository, ModelGateway
from application.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    extend_visual_components,
)
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.costs import ModelCallMetrics, ModelCostCalculator, ModelUsage, TurnMetrics
from domain.interactions import InteractionSource
from domain.tools import ToolCall, ToolCallRecord, ToolSpec
from domain.turn_events import (
    AgentAudioDelta,
    AgentAudioInterrupted,
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
    AgentVisualComponent,
    ModelAudioDelta,
    ModelAudioInterrupted,
    ModelStreamCompleted,
    ModelTextDelta,
)
from domain.visuals import VisualPresentation

logger = structlog.get_logger(__name__)


class ToolRoundsExceededError(RuntimeError):
    """Raised when a model keeps requesting tools beyond the configured limit."""

    pass


class IncompleteModelStreamError(RuntimeError):
    """Raised when a provider stream ends without a terminal accumulated reply."""

    pass


class ToolCallEvaluationRejectedError(RuntimeError):
    """Raised when an agent cannot repair a rejected tool-call batch."""

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
        tool_call_gate: ToolCallEvaluationGate | None = None,
        max_tool_call_revisions: int = 2,
        evaluation_traces: EvaluationTraceRepository | None = None,
        cost_calculator: ModelCostCalculator | None = None,
    ) -> None:
        """Initialize the orchestrator with shared gateways and the tool catalog."""
        self._model_gateway = model_gateway
        self._tools = tools
        self._conversations = conversations
        self._max_tool_rounds = max_tool_rounds
        self._tool_call_gate = tool_call_gate
        if max_tool_call_revisions < 0:
            raise ValueError("max_tool_call_revisions cannot be negative")
        self._max_tool_call_revisions = max_tool_call_revisions
        self._evaluation_traces = evaluation_traces
        self._cost_calculator = cost_calculator or ModelCostCalculator()
        self._tool_executor = ToolExecutor()

    async def run(
        self,
        message: str,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
        *,
        source: InteractionSource = "text_user",
        turn_id: str | None = None,
    ) -> AgentResult:
        """Continue and persist one owned conversation after a complete model run."""
        conversation = await self._load_conversation(conversation_key)
        effective_turn_id = turn_id or str(uuid4())
        selected_tools = self._tools.select(definition.tool_names)
        records: list[ToolCallRecord] = []
        visual_components: list[VisualPresentation] = []
        model_calls: list[ModelCallMetrics] = []
        turn_items: list[ConversationItem] = [
            ConversationMessage(role="user", content=message, source=source)
        ]

        logger.info(
            "agent_run_started",
            model=definition.model,
            message_length=len(message),
            tool_count=len(selected_tools.specs),
        )
        async with self._model_gateway.open_session(
            definition,
            selected_tools.specs,
            conversation.messages,
        ) as session:
            reply = await session.send_message(message)
            self._capture_metrics(model_calls, definition.model, reply.usage)

            tool_rounds = 0
            revisions = 0
            evaluation_attempt = 0
            terminal_rejection_sent = False
            while reply.tool_calls:
                if terminal_rejection_sent:
                    raise ToolCallEvaluationRejectedError(
                        "Agent requested another tool after a terminal evaluation result"
                    )
                logger.info(
                    "model_reply_received",
                    response_id=reply.response_id,
                    tool_call_count=len(reply.tool_calls),
                    round=tool_rounds,
                )

                evaluation_attempt += 1
                decision = await self._inspect_tool_calls(
                    context=conversation.messages + tuple(turn_items),
                    specs=selected_tools.specs,
                    calls=reply.tool_calls,
                    model_calls=model_calls,
                    conversation_key=conversation_key,
                    turn_id=effective_turn_id,
                    attempt=evaluation_attempt,
                )
                if decision is not None and not decision.execute:
                    feedback_results = decision.feedback_results
                    if decision.disposition == "inform_user":
                        terminal_rejection_sent = True
                    elif revisions >= self._max_tool_call_revisions:
                        assert self._tool_call_gate is not None
                        if self._tool_call_gate.role == "worker":
                            raise ToolCallEvaluationRejectedError(
                                "Agent could not repair rejected tool calls within the "
                                "configured limit"
                            )
                        feedback_results = tool_call_revision_limit_results(reply.tool_calls)
                        terminal_rejection_sent = True
                    else:
                        revisions += 1
                    turn_items.extend(reply.tool_calls)
                    turn_items.extend(feedback_results)
                    reply = await session.send_tool_results(feedback_results)
                    self._capture_metrics(model_calls, definition.model, reply.usage)
                    continue

                if tool_rounds >= self._max_tool_rounds:
                    raise ToolRoundsExceededError(
                        f"Agent exceeded {self._max_tool_rounds} tool rounds"
                    )

                execution = await self._tool_executor.execute(
                    reply.tool_calls,
                    selected_tools,
                    ToolExecutionContext.from_conversation(conversation_key),
                )
                records.extend(execution.records)
                extend_visual_components(visual_components, execution.visual_components)
                turn_items.extend(reply.tool_calls)
                turn_items.extend(execution.results)
                tool_rounds += 1
                reply = await session.send_tool_results(execution.results)
                self._capture_metrics(model_calls, definition.model, reply.usage)

        metrics = TurnMetrics(calls=tuple(model_calls))
        logger.info(
            "agent_run_completed",
            response_id=reply.response_id,
            tool_call_count=len(records),
            input_tokens=metrics.usage.input_tokens,
            output_tokens=metrics.usage.output_tokens,
            cached_input_tokens=metrics.usage.cached_input_tokens,
            total_tokens=metrics.usage.total_tokens,
            cost_amount=str(metrics.cost.amount) if metrics.cost is not None else None,
            cost_currency=metrics.cost.currency if metrics.cost is not None else None,
        )
        result = AgentResult(
            answer=reply.text,
            response_id=reply.response_id,
            conversation_id=conversation_key.conversation_id,
            tool_calls=tuple(records),
            visual_components=tuple(visual_components),
            metrics=metrics,
        )
        turn_items.append(
            ConversationMessage(
                role="assistant",
                content=result.answer,
                source="assistant",
                metrics=metrics if metrics.calls else None,
            )
        )
        await self._persist_turn(
            conversation,
            tuple(turn_items),
            turn_id=effective_turn_id,
        )
        return result

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        conversation_key: ConversationKey,
        *,
        source: InteractionSource = "text_user",
        turn_id: str | None = None,
    ) -> AsyncGenerator[AgentStreamEvent, None]:
        """Stream one conversation turn and persist it before terminal success."""
        conversation = await self._load_conversation(conversation_key)
        effective_turn_id = turn_id or str(uuid4())
        selected_tools = self._tools.select(definition.tool_names)
        records: list[ToolCallRecord] = []
        visual_components: list[VisualPresentation] = []
        model_calls: list[ModelCallMetrics] = []
        turn_items: list[ConversationItem] = [
            ConversationMessage(role="user", content=message, source=source)
        ]

        logger.info(
            "agent_stream_started",
            model=definition.model,
            message_length=len(message),
            tool_count=len(selected_tools.specs),
        )

        async with self._model_gateway.open_session(
            definition,
            selected_tools.specs,
            conversation.messages,
        ) as session:
            model_events = session.stream_message(message)
            tool_rounds = 0
            revisions = 0
            evaluation_attempt = 0
            terminal_rejection_sent = False
            while True:
                completed_reply = None
                async for event in model_events:
                    if isinstance(event, ModelTextDelta):
                        yield AgentTextDelta(text=event.text)
                    elif isinstance(event, ModelAudioDelta):
                        yield AgentAudioDelta(data=event.data, mime_type=event.mime_type)
                    elif isinstance(event, ModelAudioInterrupted):
                        yield AgentAudioInterrupted()
                    elif isinstance(event, ModelStreamCompleted):
                        if completed_reply is not None:
                            raise IncompleteModelStreamError(
                                "Model stream emitted more than one terminal event"
                            )
                        completed_reply = event.reply
                        self._capture_metrics(
                            model_calls,
                            definition.model,
                            completed_reply.usage,
                        )

                if completed_reply is None:
                    raise IncompleteModelStreamError(
                        "Model stream ended without a terminal response"
                    )

                logger.info(
                    "model_stream_completed",
                    response_id=completed_reply.response_id,
                    tool_call_count=len(completed_reply.tool_calls),
                    round=tool_rounds,
                )
                if not completed_reply.tool_calls:
                    metrics = TurnMetrics(calls=tuple(model_calls))
                    result = AgentResult(
                        answer=completed_reply.text,
                        response_id=completed_reply.response_id,
                        conversation_id=conversation_key.conversation_id,
                        tool_calls=tuple(records),
                        visual_components=tuple(visual_components),
                        metrics=metrics,
                    )
                    turn_items.append(
                        ConversationMessage(
                            role="assistant",
                            content=result.answer,
                            source="assistant",
                            metrics=metrics if metrics.calls else None,
                        )
                    )
                    await self._persist_turn(
                        conversation,
                        tuple(turn_items),
                        turn_id=effective_turn_id,
                    )
                    logger.info(
                        "agent_stream_completed",
                        response_id=result.response_id,
                        tool_call_count=len(records),
                        input_tokens=metrics.usage.input_tokens,
                        output_tokens=metrics.usage.output_tokens,
                        cached_input_tokens=metrics.usage.cached_input_tokens,
                        total_tokens=metrics.usage.total_tokens,
                        cost_amount=(
                            str(metrics.cost.amount) if metrics.cost is not None else None
                        ),
                        cost_currency=(metrics.cost.currency if metrics.cost is not None else None),
                    )
                    yield AgentStreamCompleted(result=result)
                    return

                if terminal_rejection_sent:
                    raise ToolCallEvaluationRejectedError(
                        "Agent requested another tool after a terminal evaluation result"
                    )

                evaluation_attempt += 1
                decision = await self._inspect_tool_calls(
                    context=conversation.messages + tuple(turn_items),
                    specs=selected_tools.specs,
                    calls=completed_reply.tool_calls,
                    model_calls=model_calls,
                    conversation_key=conversation_key,
                    turn_id=effective_turn_id,
                    attempt=evaluation_attempt,
                )
                if decision is not None and not decision.execute:
                    feedback_results = decision.feedback_results
                    if decision.disposition == "inform_user":
                        terminal_rejection_sent = True
                    elif revisions >= self._max_tool_call_revisions:
                        assert self._tool_call_gate is not None
                        if self._tool_call_gate.role == "worker":
                            raise ToolCallEvaluationRejectedError(
                                "Agent could not repair rejected tool calls within the "
                                "configured limit"
                            )
                        feedback_results = tool_call_revision_limit_results(
                            completed_reply.tool_calls
                        )
                        terminal_rejection_sent = True
                    else:
                        revisions += 1
                    turn_items.extend(completed_reply.tool_calls)
                    turn_items.extend(feedback_results)
                    model_events = session.stream_tool_results(feedback_results)
                    continue

                if tool_rounds >= self._max_tool_rounds:
                    raise ToolRoundsExceededError(
                        f"Agent exceeded {self._max_tool_rounds} tool rounds"
                    )

                for call in completed_reply.tool_calls:
                    yield AgentToolStarted(call_id=call.call_id, tool_name=call.tool_name)
                execution = await self._tool_executor.execute(
                    completed_reply.tool_calls,
                    selected_tools,
                    ToolExecutionContext.from_conversation(conversation_key),
                )
                records.extend(execution.records)
                extend_visual_components(visual_components, execution.visual_components)
                turn_items.extend(completed_reply.tool_calls)
                turn_items.extend(execution.results)
                tool_rounds += 1
                for record in execution.records:
                    yield AgentToolCompleted(record=record)
                for presentation in execution.visual_components:
                    yield AgentVisualComponent(presentation=presentation)
                model_events = session.stream_tool_results(execution.results)

        raise AssertionError("Unreachable")

    def _capture_metrics(
        self,
        calls: list[ModelCallMetrics],
        model: str,
        usage: ModelUsage,
    ) -> None:
        """Retain one billable provider request when it reports any usage."""
        if usage.total_tokens == 0:
            return
        calls.append(self._cost_calculator.metrics(model, usage))

    async def _inspect_tool_calls(
        self,
        *,
        context: tuple[ConversationItem, ...],
        specs: tuple[ToolSpec, ...],
        calls: tuple[ToolCall, ...],
        model_calls: list[ModelCallMetrics],
        conversation_key: ConversationKey,
        turn_id: str,
        attempt: int,
    ) -> ToolCallGateDecision | None:
        """Evaluate one normalized batch and include evaluator usage in turn metrics."""
        if self._tool_call_gate is None:
            return None
        decision = await self._tool_call_gate.inspect(
            context=context,
            available_tools=specs,
            proposed_calls=calls,
        )
        if decision.evaluation is not None:
            self._capture_metrics(
                model_calls,
                decision.evaluation.model,
                decision.evaluation.usage,
            )
            await append_evaluation_trace(
                self._evaluation_traces,
                decision=decision,
                conversation_id=conversation_key.conversation_id,
                turn_id=turn_id,
                job_id=turn_id if self._tool_call_gate.role == "worker" else None,
                attempt=attempt,
                calls=calls,
            )
        return decision

    async def _load_conversation(self, key: ConversationKey) -> Conversation:
        """Load an explicitly created conversation before invoking the model."""
        conversation = await self._conversations.load(key)
        if conversation is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        return conversation

    async def _persist_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
        *,
        turn_id: str,
    ) -> None:
        """Append one complete model/tool turn and save it with optimistic concurrency."""
        await self._conversations.save_turn(conversation, turn, turn_id=turn_id)
