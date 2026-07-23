import json
from collections import deque
from types import SimpleNamespace
from typing import Any

from domain.agent import AgentDefinition
from domain.conversations import ConversationMessage
from domain.tools import ToolCall, ToolResult, ToolSpec
from domain.turn_events import (
    ModelStreamCompleted,
    ModelTextDelta,
)
from infrastructure.openai_gateway import OpenAIResponsesGateway


class FakeOutputItem:
    def __init__(
        self,
        *,
        item_type: str,
        call_id: str = "",
        name: str = "",
        arguments: str = "",
    ) -> None:
        self.type = item_type
        self.call_id = call_id
        self.name = name
        self.arguments = arguments

    def model_dump(
        self,
        *,
        exclude_none: bool,
        exclude: set[str],
    ) -> dict[str, Any]:
        assert exclude_none is True
        assert exclude == {"parsed_arguments"}
        payload = {
            "type": self.type,
            "call_id": self.call_id,
            "name": self.name,
            "arguments": self.arguments,
            "parsed_arguments": json.loads(self.arguments) if self.arguments else {},
        }
        return {key: value for key, value in payload.items() if key not in exclude}


class FakeResponse:
    def __init__(self, response_id: str, text: str, output: list[FakeOutputItem]) -> None:
        self.id = response_id
        self.output_text = text
        self.output = output
        self._request_id = f"request_{response_id}"


class FakeResponsesResource:
    def __init__(
        self,
        responses: list[FakeResponse],
        stream_responses: list[tuple[list[object], FakeResponse]] | None = None,
    ) -> None:
        self.responses = deque(responses)
        self.stream_responses = deque(stream_responses or [])
        self.requests: list[dict[str, Any]] = []
        self.stream_requests: list[dict[str, Any]] = []
        self.streams: list[FakeStreamManager] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        return self.responses.popleft()

    def stream(self, **kwargs: Any) -> "FakeStreamManager":
        self.stream_requests.append(kwargs)
        events, response = self.stream_responses.popleft()
        manager = FakeStreamManager(events, response)
        self.streams.append(manager)
        return manager


class FakeStreamManager:
    def __init__(self, events: list[object], response: FakeResponse) -> None:
        self.events = events
        self.response = response
        self.closed = False

    async def __aenter__(self) -> "FakeStreamManager":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for event in self.events:
            yield event

    async def get_final_response(self) -> FakeResponse:
        return self.response


def definition() -> AgentDefinition:
    return AgentDefinition(
        model="test-model",
        instructions="Use tools when necessary",
        tool_names=("calculator",),
    )


def calculator_spec() -> ToolSpec:
    return ToolSpec(
        name="calculator",
        description="Adds numbers",
        arguments_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    )


async def test_openai_session_translates_tool_calls_and_results() -> None:
    resource = FakeResponsesResource(
        [
            FakeResponse(
                "resp_1",
                "",
                [
                    FakeOutputItem(
                        item_type="function_call",
                        call_id="call_1",
                        name="calculator",
                        arguments='{"a": 2, "b": 3}',
                    )
                ],
            ),
            FakeResponse("resp_2", "El resultado es 5.", []),
        ]
    )
    client = SimpleNamespace(responses=resource)
    async with OpenAIResponsesGateway(client).open_session(
        definition(), (calculator_spec(),), ()
    ) as session:
        first_reply = await session.send_message("Suma 2 y 3")
        final_reply = await session.send_tool_results(
            (ToolResult(call_id="call_1", output={"result": 5}),)
        )

    assert first_reply.tool_calls[0].tool_name == "calculator"
    assert first_reply.tool_calls[0].arguments == {"a": 2, "b": 3}
    assert final_reply.text == "El resultado es 5."
    first_request = resource.requests[0]
    assert first_request["model"] == "test-model"
    assert first_request["instructions"] == "Use tools when necessary"
    assert first_request["tools"][0]["type"] == "function"
    assert first_request["tools"][0]["strict"] is True
    second_input = resource.requests[1]["input"]
    assert "parsed_arguments" not in second_input[1]
    assert second_input[-1]["type"] == "function_call_output"
    assert json.loads(second_input[-1]["output"]) == {
        "ok": True,
        "result": {"result": 5},
    }


async def test_gateway_creates_isolated_sessions_with_a_shared_client() -> None:
    resource = FakeResponsesResource(
        [
            FakeResponse("resp_a", "A", []),
            FakeResponse("resp_b", "B", []),
        ]
    )
    client = SimpleNamespace(responses=resource)
    gateway = OpenAIResponsesGateway(client)
    async with (
        gateway.open_session(definition(), (), ()) as session_a,
        gateway.open_session(definition(), (), ()) as session_b,
    ):
        await session_a.send_message("Mensaje A")
        await session_b.send_message("Mensaje B")

    assert resource.requests[0]["input"] == [{"role": "user", "content": "Mensaje A"}]
    assert resource.requests[1]["input"] == [{"role": "user", "content": "Mensaje B"}]


async def test_openai_stream_normalizes_deltas_and_continues_after_a_tool() -> None:
    tool_response = FakeResponse(
        "resp_1",
        "",
        [
            FakeOutputItem(
                item_type="function_call",
                call_id="call_1",
                name="calculator",
                arguments='{"a": 2, "b": 3}',
            )
        ],
    )
    final_response = FakeResponse("resp_2", "El resultado es 5.", [])
    resource = FakeResponsesResource(
        [],
        stream_responses=[
            ([], tool_response),
            (
                [
                    SimpleNamespace(type="response.output_text.delta", delta="El resultado"),
                    SimpleNamespace(type="response.output_text.delta", delta=" es 5."),
                ],
                final_response,
            ),
        ],
    )
    client = SimpleNamespace(responses=resource)
    async with OpenAIResponsesGateway(client).open_session(
        definition(), (calculator_spec(),), ()
    ) as session:
        first_events = [event async for event in session.stream_message("Suma 2 y 3")]
        second_events = [
            event
            async for event in session.stream_tool_results(
                (ToolResult(call_id="call_1", output={"result": 5}),)
            )
        ]

    assert isinstance(first_events[-1], ModelStreamCompleted)
    assert first_events[-1].reply.tool_calls[0].tool_name == "calculator"
    assert [event.text for event in second_events if isinstance(event, ModelTextDelta)] == [
        "El resultado",
        " es 5.",
    ]
    assert isinstance(second_events[-1], ModelStreamCompleted)
    assert second_events[-1].reply.text == "El resultado es 5."
    assert all(stream.closed for stream in resource.streams)
    second_input = resource.stream_requests[1]["input"]
    assert "parsed_arguments" not in second_input[1]
    assert second_input[-1]["type"] == "function_call_output"


async def test_openai_session_translates_neutral_history_to_provider_input() -> None:
    resource = FakeResponsesResource([FakeResponse("resp_2", "Continuación", [])])
    client = SimpleNamespace(responses=resource)
    history = (
        ConversationMessage(role="user", content="Hola"),
        ToolCall(
            call_id="call_old",
            tool_name="calculator",
            arguments={"a": 2, "b": 3},
        ),
        ToolResult(call_id="call_old", output={"result": 5}),
        ConversationMessage(role="assistant", content="El resultado es 5."),
    )
    async with OpenAIResponsesGateway(client).open_session(definition(), (), history) as session:
        await session.send_message("Continúa")

    assert resource.requests[0]["input"] == [
        {"role": "user", "content": "Hola"},
        {
            "type": "function_call",
            "call_id": "call_old",
            "name": "calculator",
            "arguments": '{"a": 2, "b": 3}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_old",
            "output": '{"ok": true, "result": {"result": 5}}',
        },
        {"role": "assistant", "content": "El resultado es 5."},
        {"role": "user", "content": "Continúa"},
    ]
