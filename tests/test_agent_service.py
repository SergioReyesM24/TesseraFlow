import asyncio
from collections import deque
from collections.abc import AsyncIterator

import pytest

from application.agent import AgentService
from application.conversations import ConversationConflictError, ConversationNotFoundError
from domain.agent import AgentDefinition
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from domain.events import (
    AgentStreamCompleted,
    AgentTextDelta,
    AgentToolCompleted,
    AgentToolStarted,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from domain.model import ModelReply
from domain.tools import (
    ToolCall,
    ToolResult,
    ToolSpec,
)
from tools.registry import build_tool_registry


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

    def create_session(
        self,
        definition: AgentDefinition,
        tools: tuple[ToolSpec, ...],
        history: tuple[ConversationItem, ...],
    ) -> StubModelSession:
        session = StubModelSession(self.session_replies.popleft())
        self.sessions.append(session)
        self.definitions.append(definition)
        self.tool_specs.append(tools)
        self.histories.append(history)
        return session


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
    ) -> Conversation:
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


def conversation_key(conversation_id: str = "conversation-1") -> ConversationKey:
    return ConversationKey(conversation_id=conversation_id, user_id="user-1")


def build_service(
    gateway: StubModelGateway,
    repository: InMemoryConversationRepository | None = None,
) -> AgentService:
    return AgentService(
        gateway,
        build_tool_registry(),
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
                            tool_name="calculator",
                            arguments={"operation": "add", "a": "2.5", "b": "3"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="El resultado es 5.5."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    definition = agent_definition("calculator")
    await repository.create(conversation_key())

    result = await service.run("Suma 2.5 y 3", definition, conversation_key())

    assert result.answer == "El resultado es 5.5."
    assert result.response_id == "resp_2"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].status == "success"
    assert result.tool_calls[0].output == {"result": "5.5"}
    assert gateway.definitions == [definition]
    assert [spec.name for spec in gateway.tool_specs[0]] == ["calculator"]
    assert gateway.sessions[0].tool_result_batches == [
        (ToolResult(call_id="call_1", output={"result": "5.5"}),)
    ]
    assert repository.conversations["conversation-1"].messages == (
        ConversationMessage(role="user", content="Suma 2.5 y 3"),
        ToolCall(
            call_id="call_1",
            tool_name="calculator",
            arguments={"operation": "add", "a": "2.5", "b": "3"},
        ),
        ToolResult(call_id="call_1", output={"result": "5.5"}),
        ConversationMessage(role="assistant", content="El resultado es 5.5."),
    )


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
                            tool_name="calculator",
                            arguments={"operation": "divide", "a": "1", "b": "0"},
                        ),
                    ),
                ),
                ModelReply(response_id="resp_2", text="No se puede dividir entre cero."),
            ]
        ]
    )
    repository = InMemoryConversationRepository()
    service = build_service(gateway, repository)
    await repository.create(conversation_key())

    result = await service.run(
        "Divide uno entre cero", agent_definition("calculator"), conversation_key()
    )

    assert result.tool_calls[0].status == "error"
    assert result.tool_calls[0].error == "Cannot divide by zero"
    assert gateway.sessions[0].tool_result_batches == [
        (ToolResult(call_id="call_1", error="Cannot divide by zero"),)
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
                            tool_name="calculator",
                            arguments={"operation": "add", "a": "2", "b": "3"},
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
            "Suma 2 y 3", agent_definition("calculator"), conversation_key()
        )
    ]

    assert isinstance(events[0], AgentToolStarted)
    assert isinstance(events[1], AgentToolCompleted)
    assert isinstance(events[2], AgentTextDelta)
    assert events[2].text == "El resultado es 5."
    assert isinstance(events[3], AgentStreamCompleted)
    assert events[3].result.answer == "El resultado es 5."
    assert events[3].result.tool_calls[0].output == {"result": "5"}


def test_tool_specs_are_provider_neutral_and_closed() -> None:
    specs = build_tool_registry().specs

    assert [spec.name for spec in specs] == [
        "calculator",
        "current_time",
        "weekly_balance_history",
        "send_mock_bizum_to_mom",
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
        ConversationMessage(role="assistant", content="Primera respuesta"),
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
