import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import uuid4

import structlog

from application.ports import AgentStepEvaluator, EvaluationTraceRepository
from domain.conversations import ConversationItem
from domain.evaluations import (
    AgentRole,
    AgentStepEvaluation,
    AgentStepEvaluationRequest,
    EvaluationMode,
    EvaluationReasonCode,
    EvaluationTrace,
)
from domain.tools import ToolCall, ToolResult, ToolSpec

logger = structlog.get_logger(__name__)

ToolCallDisposition = Literal["execute", "revise_silently", "inform_user"]
TechnicalFailureMode = Literal["raise", "feedback"]


@dataclass(frozen=True, slots=True)
class ToolCallGateDecision:
    """Application decision for one complete proposed tool-call batch."""

    execute: bool
    disposition: ToolCallDisposition
    mode: EvaluationMode
    latency_ms: float
    evaluation: AgentStepEvaluation | None = None
    feedback_results: tuple[ToolResult, ...] = ()


class ToolCallEvaluationGate:
    """Apply runtime policy to model-based tool-call evaluations."""

    def __init__(
        self,
        evaluator: AgentStepEvaluator,
        *,
        role: AgentRole,
        mode: EvaluationMode,
        fail_open: bool,
        technical_failure_mode: TechnicalFailureMode = "raise",
    ) -> None:
        self._evaluator = evaluator
        self._role = role
        self._mode = mode
        self._fail_open = fail_open
        self._technical_failure_mode = technical_failure_mode

    @property
    def role(self) -> AgentRole:
        """Expose the neutral agent role needed to correlate audit traces."""
        return self._role

    async def inspect(
        self,
        *,
        context: tuple[ConversationItem, ...],
        available_tools: tuple[ToolSpec, ...],
        proposed_calls: tuple[ToolCall, ...],
    ) -> ToolCallGateDecision:
        """Evaluate a batch without leaking conversation or arguments to logs."""
        started_at = perf_counter()
        try:
            evaluation = await self._evaluator.evaluate(
                AgentStepEvaluationRequest(
                    role=self._role,
                    context=context,
                    available_tools=available_tools,
                    proposed_calls=proposed_calls,
                )
            )
        except Exception as exc:
            logger.warning(
                "tool_call_evaluation_failed",
                role=self._role,
                error_type=type(exc).__name__,
                fail_open=self._fail_open,
            )
            if self._fail_open:
                return ToolCallGateDecision(
                    execute=True,
                    disposition="execute",
                    mode=self._mode,
                    latency_ms=(perf_counter() - started_at) * 1_000,
                )
            if self._technical_failure_mode == "feedback":
                return ToolCallGateDecision(
                    execute=False,
                    disposition="inform_user",
                    mode=self._mode,
                    latency_ms=(perf_counter() - started_at) * 1_000,
                    feedback_results=tool_call_evaluation_unavailable_results(proposed_calls),
                )
            raise

        latency_ms = (perf_counter() - started_at) * 1_000
        should_execute = self._mode == "shadow" or evaluation.passed
        logger.info(
            "tool_call_evaluated",
            role=self._role,
            mode=self._mode,
            verdict=evaluation.verdict,
            risk=evaluation.risk,
            reason_code=evaluation.reason_code,
            tool_call_count=len(proposed_calls),
            executed=should_execute,
        )
        if should_execute:
            return ToolCallGateDecision(
                execute=True,
                disposition="execute",
                mode=self._mode,
                latency_ms=latency_ms,
                evaluation=evaluation,
            )
        return ToolCallGateDecision(
            execute=False,
            disposition="revise_silently",
            mode=self._mode,
            latency_ms=latency_ms,
            evaluation=evaluation,
            feedback_results=self._feedback_results(proposed_calls, evaluation),
        )

    @staticmethod
    def _feedback_results(
        calls: tuple[ToolCall, ...],
        evaluation: AgentStepEvaluation,
    ) -> tuple[ToolResult, ...]:
        """Reject every pending call so the provider session can request a new batch."""
        error = json.dumps(
            {
                "code": "tool_call_rejected",
                "verdict": evaluation.verdict,
                "risk": evaluation.risk,
                "reason_code": evaluation.reason_code,
                "feedback": evaluation.feedback,
                "disposition": "revise_silently",
                "instruction": (
                    "The rejected tool call did not execute. Silently emit a corrected "
                    "tool-call batch before any user-facing completion text or audio. Do not "
                    "end the turn or claim success until the corrected call executes. Do not "
                    "mention this internal rejection to the user."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return tuple(ToolResult(call_id=call.call_id, error=error) for call in calls)


def tool_call_evaluation_unavailable_results(
    calls: tuple[ToolCall, ...],
) -> tuple[ToolResult, ...]:
    """Tell the agent that policy infrastructure failed without leaking internals."""
    return _policy_error_results(
        calls,
        code="tool_call_evaluation_unavailable",
        instruction=(
            "Do not call another tool in this turn. Briefly tell the user that the operation "
            "could not be validated and can be tried again. Do not expose internal details."
        ),
    )


def tool_call_revision_limit_results(calls: tuple[ToolCall, ...]) -> tuple[ToolResult, ...]:
    """End a rejected repair loop through a model-visible, user-safe terminal result."""
    return _policy_error_results(
        calls,
        code="tool_call_revision_limit_exceeded",
        instruction=(
            "Do not call another tool in this turn. Briefly tell the user that the operation "
            "could not be completed and can be tried again. Do not expose internal details."
        ),
    )


def _policy_error_results(
    calls: tuple[ToolCall, ...],
    *,
    code: str,
    instruction: str,
) -> tuple[ToolResult, ...]:
    """Build one complete terminal batch for a non-repairable policy outcome."""
    error = json.dumps(
        {
            "code": code,
            "disposition": "inform_user",
            "instruction": instruction,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return tuple(ToolResult(call_id=call.call_id, error=error) for call in calls)


_REDACTED_FEEDBACK: dict[EvaluationReasonCode, str] = {
    "none": "",
    "wrong_tool": "Selecciona una herramienta adecuada para la petición actual.",
    "unnecessary_tool": "Evita herramientas que no sean necesarias para completar la petición.",
    "ungrounded_arguments": "Usa únicamente argumentos respaldados por el contexto disponible.",
    "duplicate_action": "No repitas una acción que ya se haya completado.",
    "unsafe_side_effect": "Revisa los efectos secundarios y elige una acción segura.",
    "incomplete_request": "Completa la propuesta con la información necesaria antes de ejecutarla.",
    "other": "Revisa la propuesta de herramientas antes de volver a intentarlo.",
}


def redacted_evaluation_feedback(reason_code: EvaluationReasonCode) -> str:
    """Return a deterministic audit-safe summary without model-supplied data."""
    return _REDACTED_FEEDBACK[reason_code]


async def append_evaluation_trace(
    repository: EvaluationTraceRepository | None,
    *,
    decision: ToolCallGateDecision,
    conversation_id: str,
    turn_id: str,
    job_id: str | None,
    attempt: int,
    calls: tuple[ToolCall, ...],
) -> None:
    """Persist one evaluated proposal without exposing provider feedback to audit views."""
    evaluation = decision.evaluation
    if repository is None or evaluation is None:
        return
    await repository.append_evaluation_trace(
        EvaluationTrace(
            trace_id=str(uuid4()),
            conversation_id=conversation_id,
            turn_id=turn_id,
            job_id=job_id,
            attempt=attempt,
            proposed_call_ids=tuple(call.call_id for call in calls),
            verdict=evaluation.verdict,
            risk=evaluation.risk,
            reason_code=evaluation.reason_code,
            feedback=redacted_evaluation_feedback(evaluation.reason_code),
            mode=decision.mode,
            executed=decision.execute,
            model=evaluation.model,
            usage=evaluation.usage,
            latency_ms=decision.latency_ms,
            created_at=datetime.now(UTC),
        )
    )
