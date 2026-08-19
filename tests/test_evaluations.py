import pytest

from application.evaluations import ToolCallEvaluationGate
from domain.conversations import ConversationMessage
from domain.evaluations import AgentStepEvaluation, AgentStepEvaluationRequest
from domain.tools import ToolCall, ToolSpec


class FailingEvaluator:
    async def evaluate(self, request: AgentStepEvaluationRequest) -> AgentStepEvaluation:
        del request
        raise TimeoutError("evaluation timed out")


class RejectingEvaluator:
    async def evaluate(self, request: AgentStepEvaluationRequest) -> AgentStepEvaluation:
        del request
        return AgentStepEvaluation(
            verdict="fail",
            risk="high",
            reason_code="unsafe_side_effect",
            feedback="Remove the unsupported side effect.",
            model="evaluation-model",
        )


def evidence() -> tuple[
    tuple[ConversationMessage, ...],
    tuple[ToolSpec, ...],
    tuple[ToolCall, ...],
]:
    return (
        (ConversationMessage(role="user", content="Inspect data"),),
        (
            ToolSpec(
                name="lookup",
                description="Read data.",
                arguments_schema={"type": "object"},
            ),
        ),
        (
            ToolCall(call_id="call-1", tool_name="lookup", arguments={}),
            ToolCall(call_id="call-2", tool_name="lookup", arguments={}),
        ),
    )


async def test_fail_open_allows_execution_after_an_evaluator_failure() -> None:
    context, tools, calls = evidence()
    gate = ToolCallEvaluationGate(
        FailingEvaluator(),
        role="worker",
        mode="enforce",
        fail_open=True,
    )

    decision = await gate.inspect(
        context=context,
        available_tools=tools,
        proposed_calls=calls,
    )

    assert decision.execute is True
    assert decision.evaluation is None


async def test_enforced_rejection_aborts_the_complete_batch() -> None:
    context, tools, calls = evidence()
    gate = ToolCallEvaluationGate(
        RejectingEvaluator(),
        role="worker",
        mode="enforce",
        fail_open=False,
    )

    decision = await gate.inspect(
        context=context,
        available_tools=tools,
        proposed_calls=calls,
    )

    assert decision.execute is False
    assert [result.call_id for result in decision.feedback_results] == ["call-1", "call-2"]
    assert all(result.error is not None for result in decision.feedback_results)
    assert all(
        "did not execute" in result.error
        and "corrected tool-call batch" in result.error
        and "claim success" in result.error
        for result in decision.feedback_results
        if result.error is not None
    )


async def test_fail_closed_propagates_an_evaluator_failure() -> None:
    context, tools, calls = evidence()
    gate = ToolCallEvaluationGate(
        FailingEvaluator(),
        role="worker",
        mode="enforce",
        fail_open=False,
    )

    with pytest.raises(TimeoutError, match="evaluation timed out"):
        await gate.inspect(
            context=context,
            available_tools=tools,
            proposed_calls=calls,
        )


async def test_interactive_fail_closed_returns_model_visible_general_failure() -> None:
    """Let the interactive agent explain an evaluator outage without executing a tool."""
    context, tools, calls = evidence()
    gate = ToolCallEvaluationGate(
        FailingEvaluator(),
        role="interactive",
        mode="enforce",
        fail_open=False,
        technical_failure_mode="feedback",
    )

    decision = await gate.inspect(
        context=context,
        available_tools=tools,
        proposed_calls=calls,
    )

    assert decision.execute is False
    assert decision.disposition == "inform_user"
    assert all(result.error is not None for result in decision.feedback_results)
    assert all(
        '"code":"tool_call_evaluation_unavailable"' in result.error
        for result in decision.feedback_results
        if result.error is not None
    )
