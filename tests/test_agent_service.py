import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import ClassVar

import pytest

from application.agent import AgentService, ToolCallEvaluationRejectedError
from application.conversations import ConversationConflictError, ConversationNotFoundError
from application.evaluations import ToolCallEvaluationGate
from application.tools import AgentTool, ToolArguments, ToolExecutionContext, ToolRegistry
from domain.agent import AgentDefinition
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.costs import ModelCostCalculator, ModelRates, ModelUsage
from domain.evaluations import AgentStepEvaluation, AgentStepEvaluationRequest, EvaluationTrace
from domain.model import ModelReply
from domain.tools import (
    ToolCall,
    ToolResult,
    ToolSpec,
)
from domain.turn_events import (
    AgentAudioDelta,
    AgentAudioInterrupted,
    AgentStreamCompleted,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
    AgentVisualComponent,
    ModelAudioDelta,
    ModelAudioInterrupted,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from tools.present_visual import PresentVisualTool
from tools.registry import build_tool_registry
from tools.weekly_balance_history import WeeklyBalanceHistoryTool


class StubModelSession:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = deque(replies)
        self.messages: list[str] = []
        self.tool_result_batches: list[tuple[ToolResult, ...]] = []

    async def send_message(self, message: str) -> ModelReply:
        self.messages.append(message)
        return self.replies.popleft()

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        self.tool_result_batches.append(results)
        return self.replies.popleft()

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        self.messages.append(message)
        return self._stream_next_reply()

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        self.tool_result_batches.append(results)
        return self._stream_next_reply()

    async def _stream_next_reply(self) -> AsyncIterator[ModelStreamEvent]:
        reply = self.replies.popleft()
        if reply.text:
            yield ModelTextDelta(text=reply.text)
        yield ModelStreamCompleted(reply=reply)


class StubModelGateway:
    def __init__(self, session_replies: list[list[ModelReply]]) -> None:
        self.session_replies = deque(session_replies)
        self.sessions: list[StubModelSession] = []
        self.definitions: list[AgentDefinition] = []
        self.tool_specs: list[tuple[ToolSpec, ...]] = []
        self.histories: list[tuple[ConversationItem, ...]] = []

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncGenerator[StubModelSession, None]:
        session = StubModelSession(self.session_replies.popleft())
        self.sessions.append(session)
        self.definitions.append(definition)
        self.tool_specs.append(tools)
        self.histories.append(history)
        yield session


class AudioStubModelSession:
    """Emit native audio through the common model-session contract."""

    async def send_message(self, message: str) -> ModelReply:
        """Provide the non-streaming method unused by this test."""
        del message
        return ModelReply(response_id="audio-1", text="Respuesta hablada")

    async def send_tool_results(self, results: tuple[ToolResult, ...]) -> ModelReply:
        """Provide the continuation method unused by this test."""
        del results
        return ModelReply(response_id="audio-1", text="Respuesta hablada")

    def stream_message(self, message: str) -> AsyncIterator[ModelStreamEvent]:
        """Stream PCM, interruption, transcription, and common completion."""
        del message
        return self._stream()

    def stream_tool_results(
        self,
        results: tuple[ToolResult, ...],
    ) -> AsyncIterator[ModelStreamEvent]:
        """Provide the continuation stream unused by this test."""
        del results
        return self._stream()

    async def _stream(self) -> AsyncIterator[ModelStreamEvent]:
        """Emit one complete native-audio response."""
        reply = ModelReply(response_id="audio-1", text="Respuesta hablada")
        yield ModelAudioDelta(data=b"pcm", mime_type="audio/pcm;rate=24000")
        yield ModelAudioInterrupted()
        yield ModelTextDelta(text=reply.text)
        yield ModelStreamCompleted(reply=reply)


class AudioStubModelGateway:
    """Open one deterministic native-audio model session."""

    @asynccontextmanager
    async def open_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> AsyncGenerator[AudioStubModelSession, None]:
        """Yield an isolated session without provider resources."""
        del definition, tools, history
        yield AudioStubModelSession()


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self.conversations: dict[str, Conversation] = {}

    async def create(self, key: ConversationKey) -> Conversation:
        conversation = Conversation(key=key)
        self.conversations[key.conversation_id] = conversation
        return conversation

    async def load(self, key: ConversationKey) -> Conversation | None:
        conversation = self.conversations.get(key.conversation_id)
        if conversation is not None and conversation.key != key:
            raise PermissionError
        return conversation

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
        *,
        turn_id: str,
    ) -> Conversation:
        del turn_id
        current = self.conversations.get(conversation.key.conversation_id)
        current_version = current.version if current is not None else 0
        if current_version != conversation.version:
            raise ConversationConflictError
        saved = Conversation(
            key=conversation.key,
            messages=conversation.messages + turn,
            version=conversation.version + 1,
            title=conversation.title,
        )
        self.conversations[conversation.key.conversation_id] = saved
        return saved

    async def delete(self, key: ConversationKey) -> bool:
        return self.conversations.pop(key.conversation_id, None) is not None


class SampleToolArguments(ToolArguments):
    """Inputs for the test-only tool used by agent orchestration tests."""

    result: str
    error: str | None = None


class SampleTool(AgentTool[SampleToolArguments]):
    """Return a value or a requested error without depending on product tools."""

    name = "sample_tool"
    description = "Test-only deterministic tool."
    arguments_model: ClassVar[type[SampleToolArguments]] = SampleToolArguments

    def __init__(self) -> None:
        self.invocations: list[SampleToolArguments] = []

    async def execute(
        self,
        arguments: SampleToolArguments,
        context: ToolExecutionContext,
    ) -> object:
        del context
        self.invocations.append(arguments)
        if arguments.error is not None:
            raise ValueError(arguments.error)
        return {"result": arguments.result}


class StubStepEvaluator:
    """Return deterministic judgments while retaining neutral evidence."""

    def __init__(self, evaluations: list[AgentStepEvaluation]) -> None:
        self.evaluations = deque(evaluations)
        self.requests: list[AgentStepEvaluationRequest] = []

    async def evaluate(self, request: AgentStepEvaluationRequest) -> AgentStepEvaluation:
        self.requests.append(request)
        return self.evaluations.popleft()


class InMemoryEvaluationTraceRepository:
    def __init__(self) -> None:
        self.traces: list[EvaluationTrace] = []

    async def append_evaluation_trace(self, trace: EvaluationTrace) -> None:
        self.traces.append(trace)


def conversation_key(conversation_id: str = "conversation-1") -> ConversationKey:
    return ConversationKey(conversation_id=conversation_id, user_id="user-1")


def build_service(
    gateway: StubModelGateway,
    repository: InMemoryConversationRepository | None = None,
) -> AgentService:
    return AgentService(
        gateway,
        ToolRegistry([SampleTool()]),
        repository or InMemoryConversationRepository(),
    )


def agent_definition(*tool_names: str) -> AgentDefinition:
    return AgentDefinition(
        model="test-model",
        instructions="Test instructions",
        tool_names=tool_names,
    )


async def test_runs_and_captures_a_tool_call() -> None:
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_1",
                            tool_name="sample_tool",
                            arguments={"result": "5.5"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="El resultado es 5.5."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    definition = agent_definition("sample_tool")
    await repository.create(conversation_key())

    result = await service.run("Suma 2.5 y 3", definition, conversation_key())

    assert result.answer == "El resultado es 5.5."
    assert result.response_id == "resp_2"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].status == "success"
    assert result.tool_calls[0].output == {"result": "5.5"}
    assert gateway.definitions == [definition]
    assert [spec.name for spec in gateway.tool_specs[0]] == ["sample_tool"]
    assert gateway.sessions[0].tool_result_batches == [
        (ToolResult(call_id="call_1", output={"result": "5.5"}),)
    ]
    assert repository.conversations["conversation-1"].messages == (
        ConversationMessage(role="user", content="Suma 2.5 y 3"),
        ToolCall(
            call_id="call_1",
            tool_name="sample_tool",
            arguments={"result": "5.5"},
        ),
        ToolResult(call_id="call_1", output={"result": "5.5"}),
        ConversationMessage(
            role="assistant",
            content="El resultado es 5.5.",
            source="assistant",
        ),
    )


async def test_rejected_tool_call_is_revised_before_any_tool_executes() -> None:
    """Feed a rejected batch back to the model and execute only its valid replacement."""
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_bad",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_bad",
                            tool_name="sample_tool",
                            arguments={"result": "invented"},
                        ),
                    ),
                ),
                ModelReply(
                    response_id="resp_fixed",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_fixed",
                            tool_name="sample_tool",
                            arguments={"result": "supported"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_done", text="Resultado comprobado"),
            ]
        ]
    )
    evaluator = StubStepEvaluator(
        [
            AgentStepEvaluation(
                verdict="fail",
                risk="medium",
                reason_code="ungrounded_arguments",
                feedback="Use only arguments supported by the request.",
                model="evaluation-model",
                usage=ModelUsage(input_tokens=20, output_tokens=5),
            ),
            AgentStepEvaluation(
                verdict="pass",
                risk="low",
                reason_code="none",
                feedback="",
                model="evaluation-model",
                usage=ModelUsage(input_tokens=20, output_tokens=5),
            ),
        ]
    )
    tool = SampleTool()
    repository = InMemoryConversationRepository()
    traces = InMemoryEvaluationTraceRepository()
    service = AgentService(
        gateway,
        ToolRegistry([tool]),
        repository,
        tool_call_gate=ToolCallEvaluationGate(
            evaluator,
            role="worker",
            mode="enforce",
            fail_open=False,
        ),
        evaluation_traces=traces,
    )
    await repository.create(conversation_key())

    result = await service.run(
        "Use a supported value",
        agent_definition("sample_tool"),
        conversation_key(),
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert [arguments.result for arguments in tool.invocations] == ["supported"]
    assert [record.call_id for record in result.tool_calls] == ["call_fixed"]
    assert [call.model for call in result.metrics.calls] == [
        "evaluation-model",
        "evaluation-model",
    ]
    rejected_result = gateway.sessions[0].tool_result_batches[0][0]
    assert rejected_result.call_id == "call_bad"
    assert rejected_result.error is not None
    assert '"code":"tool_call_rejected"' in rejected_result.error
    assert evaluator.requests[1].context[-2:] == (
        ToolCall(
            call_id="call_bad",
            tool_name="sample_tool",
            arguments={"result": "invented"},
        ),
        rejected_result,
    )
    assert [trace.attempt for trace in traces.traces] == [1, 2]
    assert [trace.executed for trace in traces.traces] == [False, True]
    assert traces.traces[0].feedback == (
        "Usa únicamente argumentos respaldados por el contexto disponible."
    )
    assert traces.traces[0].proposed_call_ids == ("call_bad",)
    assert traces.traces[0].turn_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert traces.traces[0].job_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


async def test_shadow_evaluation_never_blocks_tool_execution() -> None:
    """Collect a failing judgment without changing runtime behavior in shadow mode."""
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_1",
                            tool_name="sample_tool",
                            arguments={"result": "value"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="Hecho"),
            ]
        ]
    )
    evaluator = StubStepEvaluator(
        [
            AgentStepEvaluation(
                verdict="fail",
                risk="high",
                reason_code="wrong_tool",
                feedback="Choose another tool.",
                model="evaluation-model",
            )
        ]
    )
    tool = SampleTool()
    repository = InMemoryConversationRepository()
    service = AgentService(
        gateway,
        ToolRegistry([tool]),
        repository,
        tool_call_gate=ToolCallEvaluationGate(
            evaluator,
            role="worker",
            mode="shadow",
            fail_open=False,
        ),
    )
    await repository.create(conversation_key())

    result = await service.run(
        "Run it",
        agent_definition("sample_tool"),
        conversation_key(),
    )

    assert result.answer == "Hecho"
    assert [arguments.result for arguments in tool.invocations] == ["value"]
    assert len(gateway.sessions[0].tool_result_batches) == 1


async def test_interactive_evaluation_trace_does_not_claim_a_worker_job_id() -> None:
    """Correlate a root evaluation to its turn without inventing an A2A job identity."""
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_delegate",
                            tool_name="sample_tool",
                            arguments={"result": "supported"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="Delegado"),
            ]
        ]
    )
    evaluator = StubStepEvaluator(
        [
            AgentStepEvaluation(
                verdict="pass",
                risk="low",
                reason_code="none",
                feedback="",
                model="evaluation-model",
            )
        ]
    )
    repository = InMemoryConversationRepository()
    traces = InMemoryEvaluationTraceRepository()
    service = AgentService(
        gateway,
        ToolRegistry([SampleTool()]),
        repository,
        tool_call_gate=ToolCallEvaluationGate(
            evaluator,
            role="interactive",
            mode="enforce",
            fail_open=False,
        ),
        evaluation_traces=traces,
    )
    await repository.create(conversation_key())

    await service.run(
        "Delega esta tarea",
        agent_definition("sample_tool"),
        conversation_key(),
        turn_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )

    assert len(traces.traces) == 1
    assert traces.traces[0].turn_id == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    assert traces.traces[0].job_id is None


async def test_interactive_revision_limit_becomes_a_user_facing_model_result() -> None:
    """Stop internal repair and let the main agent explain a terminal policy outcome."""
    rejected_calls = (
        ToolCall(
            call_id="call_delegate",
            tool_name="sample_tool",
            arguments={"result": "new-thread"},
        ),
        ToolCall(
            call_id="call_continue",
            tool_name="sample_tool",
            arguments={"result": "wrong-thread"},
        ),
    )
    gateway = StubModelGateway(
        [
            [
                ModelReply(response_id="resp_1", text="", tool_calls=(rejected_calls[0],)),
                ModelReply(response_id="resp_2", text="", tool_calls=(rejected_calls[1],)),
                ModelReply(
                    response_id="resp_3",
                    text="No he podido completar la operación. Puedes intentarlo de nuevo.",
                ),
            ]
        ]
    )
    evaluator = StubStepEvaluator(
        [
            AgentStepEvaluation(
                verdict="fail",
                risk="medium",
                reason_code="wrong_tool",
                feedback="Use the existing thread.",
                model="evaluation-model",
            )
            for _ in rejected_calls
        ]
    )
    tool = SampleTool()
    repository = InMemoryConversationRepository()
    service = AgentService(
        gateway,
        ToolRegistry([tool]),
        repository,
        tool_call_gate=ToolCallEvaluationGate(
            evaluator,
            role="interactive",
            mode="enforce",
            fail_open=False,
            technical_failure_mode="feedback",
        ),
        max_tool_call_revisions=1,
    )
    await repository.create(conversation_key())

    result = await service.run(
        "Continúa el trabajo",
        agent_definition("sample_tool"),
        conversation_key(),
    )

    assert tool.invocations == []
    assert result.answer.startswith("No he podido")
    terminal_result = gateway.sessions[0].tool_result_batches[1][0]
    assert terminal_result.error is not None
    assert '"code":"tool_call_revision_limit_exceeded"' in terminal_result.error
    assert '"disposition":"inform_user"' in terminal_result.error


async def test_rejected_tool_calls_stop_after_the_revision_limit() -> None:
    """Bound corrective loops and leave every rejected action unexecuted."""
    rejected_calls = [
        ToolCall(
            call_id=f"call_{index}",
            tool_name="sample_tool",
            arguments={"result": f"value-{index}"},
        )
        for index in range(2)
    ]
    gateway = StubModelGateway(
        [
            [
                ModelReply(response_id="resp_1", text="", tool_calls=(rejected_calls[0],)),
                ModelReply(response_id="resp_2", text="", tool_calls=(rejected_calls[1],)),
            ]
        ]
    )
    evaluator = StubStepEvaluator(
        [
            AgentStepEvaluation(
                verdict="fail",
                risk="medium",
                reason_code="ungrounded_arguments",
                feedback="Use supported arguments.",
                model="evaluation-model",
            )
            for _ in rejected_calls
        ]
    )
    tool = SampleTool()
    repository = InMemoryConversationRepository()
    service = AgentService(
        gateway,
        ToolRegistry([tool]),
        repository,
        tool_call_gate=ToolCallEvaluationGate(
            evaluator,
            role="worker",
            mode="enforce",
            fail_open=False,
        ),
        max_tool_call_revisions=1,
    )
    await repository.create(conversation_key())

    with pytest.raises(ToolCallEvaluationRejectedError):
        await service.run(
            "Use only grounded arguments",
            agent_definition("sample_tool"),
            conversation_key(),
        )

    assert tool.invocations == []
    assert len(evaluator.requests) == 2
    assert len(gateway.sessions[0].tool_result_batches) == 1


async def test_aggregates_tool_round_costs_and_persists_them_on_the_turn() -> None:
    """Account for every model request hidden inside one logical user turn."""
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_1",
                            tool_name="sample_tool",
                            arguments={"result": "5.5"},
                        ),
                    ),
                    usage=ModelUsage(input_tokens=100, output_tokens=10),
                ),
                ModelReply(
                    response_id="resp_2",
                    text="El resultado es 5.5.",
                    usage=ModelUsage(input_tokens=50, output_tokens=20),
                ),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = AgentService(
        gateway,
        ToolRegistry([SampleTool()]),
        repository,
        cost_calculator=ModelCostCalculator(
            {"test-model": ModelRates(input=Decimal("1"), output=Decimal("2"))}
        ),
    )
    await repository.create(conversation_key())

    result = await service.run(
        "Suma 2.5 y 3",
        agent_definition("sample_tool"),
        conversation_key(),
    )

    assert result.metrics.usage == ModelUsage(input_tokens=150, output_tokens=30)
    assert result.metrics.cost is not None
    assert result.metrics.cost.amount == Decimal("0.00021")
    assistant = repository.conversations["conversation-1"].messages[-1]
    assert isinstance(assistant, ConversationMessage)
    assert assistant.metrics == result.metrics


async def test_returns_tool_errors_to_the_model() -> None:
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_1",
                            tool_name="sample_tool",
                            arguments={"result": "", "error": "Requested failure"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="La operación ha fallado."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    await repository.create(conversation_key())

    result = await service.run("Falla", agent_definition("sample_tool"), conversation_key())

    assert result.tool_calls[0].status == "error"
    assert result.tool_calls[0].error == "Requested failure"
    assert gateway.sessions[0].tool_result_batches == [
        (ToolResult(call_id="call_1", error="Requested failure"),)
    ]


async def test_concurrent_runs_use_independent_model_sessions() -> None:
    gateway = StubModelGateway(
        [
            [ModelReply(response_id="resp_a", text="Respuesta A")],
            [ModelReply(response_id="resp_b", text="Respuesta B")],
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    definition = agent_definition()
    await repository.create(conversation_key("conversation-a"))
    await repository.create(conversation_key("conversation-b"))

    results = await asyncio.gather(
        service.run("Mensaje A", definition, conversation_key("conversation-a")),
        service.run("Mensaje B", definition, conversation_key("conversation-b")),
    )

    assert [result.answer for result in results] == ["Respuesta A", "Respuesta B"]
    assert len(gateway.sessions) == 2
    assert gateway.sessions[0] is not gateway.sessions[1]
    assert gateway.sessions[0].messages == ["Mensaje A"]
    assert gateway.sessions[1].messages == ["Mensaje B"]


async def test_streams_text_and_tool_lifecycle_events() -> None:
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_1",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_1",
                            tool_name="sample_tool",
                            arguments={"result": "5"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="El resultado es 5."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    await repository.create(conversation_key())

    events = [
        event
        async for event in service.stream(
            "Ejecuta la operación", agent_definition("sample_tool"), conversation_key()
        )
    ]

    assert isinstance(events[0], AgentToolStarted)
    assert isinstance(events[1], AgentToolCompleted)
    assert isinstance(events[2], AgentTextDelta)
    assert events[2].text == "El resultado es 5."
    assert isinstance(events[3], AgentStreamCompleted)
    assert events[3].result.answer == "El resultado es 5."
    assert events[3].result.tool_calls[0].output == {"result": "5"}


async def test_streams_visual_component_before_terminal_text_result() -> None:
    """Expose a validated presentation and retain it in the complete result."""
    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_visual",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_visual",
                            tool_name="present_visual",
                            arguments={
                                "component_id": "trend",
                                "fallback_text": "La serie sube de 10 a 12.",
                                "component": {
                                    "kind": "chart",
                                    "title": "Tendencia",
                                    "subtitle": None,
                                    "chart_type": "line",
                                    "x_label": None,
                                    "y_label": None,
                                    "y_unit": None,
                                    "series": [
                                        {
                                            "name": "Valor",
                                            "points": [
                                                {"x": "A", "y": 10},
                                                {"x": "B", "y": 12},
                                            ],
                                        }
                                    ],
                                },
                            },
                        ),
                    ),
                ),
                ModelReply(response_id="resp_done", text="La tendencia es ascendente."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = AgentService(gateway, ToolRegistry([PresentVisualTool()]), repository)
    await repository.create(conversation_key())

    events = [
        event
        async for event in service.stream(
            "Muéstrame la tendencia",
            agent_definition("present_visual"),
            conversation_key(),
        )
    ]

    assert isinstance(events[0], AgentToolStarted)
    assert isinstance(events[1], AgentToolCompleted)
    assert isinstance(events[2], AgentVisualComponent)
    assert isinstance(events[3], AgentTextDelta)
    assert isinstance(events[4], AgentStreamCompleted)
    assert events[4].result.visual_components == (events[2].presentation,)


async def test_streams_visual_declared_by_a_backend_tool_without_present_visual() -> None:
    """Route a tool-owned presentation directly while returning its data to the model."""

    async def no_sleep(delay: float) -> None:
        del delay

    gateway = StubModelGateway(
        [
            [
                ModelReply(
                    response_id="resp_lookup",
                    text="",
                    tool_calls=(
                        ToolCall(
                            call_id="call_lookup",
                            tool_name="weekly_balance_history",
                            arguments={},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_done", text="Aquí tienes el historial."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = AgentService(
        gateway,
        ToolRegistry([WeeklyBalanceHistoryTool(sleeper=no_sleep)]),
        repository,
    )
    await repository.create(conversation_key())

    events = [
        event
        async for event in service.stream(
            "Muéstrame el historial",
            agent_definition("weekly_balance_history"),
            conversation_key(),
        )
    ]

    assert isinstance(events[0], AgentToolStarted)
    assert isinstance(events[1], AgentToolCompleted)
    assert isinstance(events[2], AgentVisualComponent)
    assert events[2].presentation.component_id == "weekly-balance-history"
    assert isinstance(events[3], AgentTextDelta)
    assert isinstance(events[4], AgentStreamCompleted)
    assert events[4].result.visual_components == (events[2].presentation,)
    model_result = gateway.sessions[0].tool_result_batches[0][0].output
    assert model_result["currency"] == "€"
    assert "visual_components" not in model_result


def test_tool_specs_are_provider_neutral_and_closed() -> None:
    specs = build_tool_registry().specs

    assert [spec.name for spec in specs] == [
        "weekly_balance_history",
        "send_mock_bizum_to_mom",
        "recent_transactions",
    ]
    assert all(spec.arguments_schema["additionalProperties"] is False for spec in specs)
    assert all(not hasattr(spec, "strict") for spec in specs)


async def test_continues_a_persisted_conversation_with_neutral_history() -> None:
    gateway = StubModelGateway(
        [
            [ModelReply(response_id="resp_1", text="Primera respuesta")],
            [ModelReply(response_id="resp_2", text="Segunda respuesta")],
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    key = conversation_key()
    await repository.create(key)

    await service.run("Primer mensaje", agent_definition(), key)
    await service.run("Segundo mensaje", agent_definition(), key)

    assert gateway.histories[0] == ()
    assert gateway.histories[1] == (
        ConversationMessage(role="user", content="Primer mensaje"),
        ConversationMessage(role="assistant", content="Primera respuesta", source="assistant"),
    )
    assert repository.conversations[key.conversation_id].version == 2


async def test_stream_persists_before_emitting_completed() -> None:
    gateway = StubModelGateway([[ModelReply(response_id="resp_1", text="Respuesta")]])
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    await repository.create(conversation_key())

    events = [
        event async for event in service.stream("Mensaje", agent_definition(), conversation_key())
    ]

    assert isinstance(events[-1], AgentStreamCompleted)
    assert repository.conversations["conversation-1"].messages[-1].content == "Respuesta"


async def test_rejects_a_chat_for_an_unknown_session_before_calling_the_model() -> None:
    """Require explicit session creation before invoking a provider."""
    gateway = StubModelGateway([[ModelReply(response_id="unused", text="unused")]])
    service = build_service(gateway)

    with pytest.raises(ConversationNotFoundError):
        await service.run("Mensaje", agent_definition(), conversation_key("missing"))

    assert gateway.sessions == []


async def test_common_agent_service_streams_native_audio_events() -> None:
    """Keep tool orchestration and persistence shared across text and audio gateways."""
    repository = InMemoryConversationRepository()
    await repository.create(conversation_key())
    service = AgentService(
        AudioStubModelGateway(),
        build_tool_registry(),
        repository,
    )

    events = [
        event
        async for event in service.stream(
            "Háblame",
            agent_definition(),
            conversation_key(),
        )
    ]

    assert isinstance(events[0], AgentAudioDelta)
    assert events[0].data == b"pcm"
    assert isinstance(events[1], AgentAudioInterrupted)
    assert isinstance(events[2], AgentTextDelta)
    assert isinstance(events[-1], AgentStreamCompleted)
    assert repository.conversations["conversation-1"].messages[-1].content == ("Respuesta hablada")
