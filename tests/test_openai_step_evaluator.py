import json
from types import SimpleNamespace
from typing import Any

from domain.conversations import ConversationMessage
from domain.evaluations import AgentStepEvaluationRequest
from domain.tools import ToolCall, ToolSpec
from infrastructure.openai_step_evaluator import OpenAIToolCallEvaluator


class FakeResponses:
    """Parse the requested Pydantic schema without contacting OpenAI."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        text_format = kwargs["text_format"]
        return SimpleNamespace(
            output_parsed=text_format(
                verdict="fail",
                risk="medium",
                reason_code="wrong_tool",
                feedback="Select the tool that matches the request.",
            ),
            usage=SimpleNamespace(
                input_tokens=30,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=10),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
            _request_id="request-1",
        )


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


async def test_openai_evaluator_uses_structured_outputs_and_neutral_payload() -> None:
    client = FakeClient()
    evaluator = OpenAIToolCallEvaluator(  # type: ignore[arg-type]
        client,
        model="evaluation-model",
        instructions="Evaluate tool calls",
        timeout_seconds=1,
        reasoning_effort="none",
    )
    request = AgentStepEvaluationRequest(
        role="worker",
        context=(ConversationMessage(role="user", content="Show recent transactions"),),
        available_tools=(
            ToolSpec(
                name="recent_transactions",
                description="Returns recent transactions.",
                arguments_schema={"type": "object", "additionalProperties": False},
            ),
        ),
        proposed_calls=(ToolCall(call_id="call-1", tool_name="wrong_tool", arguments={}),),
    )

    result = await evaluator.evaluate(request)

    assert result.verdict == "fail"
    assert result.reason_code == "wrong_tool"
    assert result.usage.input_tokens == 30
    assert result.usage.cached_input_tokens == 10
    assert result.usage.reasoning_tokens == 2
    assert client.responses.kwargs["model"] == "evaluation-model"
    assert client.responses.kwargs["reasoning"]["effort"] == "none"
    assert client.responses.kwargs["store"] is False
    payload = json.loads(client.responses.kwargs["input"][0]["content"])
    assert payload["protocol"] == "tesseraflow.tool_call_evaluation"
    assert payload["conversation_context"][0]["content"] == "Show recent transactions"
    assert payload["proposed_calls"][0]["tool_name"] == "wrong_tool"
