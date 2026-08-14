from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from domain.costs import ModelUsage
from domain.tools import ToolCall, ToolSpec

if TYPE_CHECKING:
    from domain.conversations import ConversationItem

AgentRole = Literal["interactive", "worker"]
EvaluationVerdict = Literal["pass", "fail", "uncertain"]
EvaluationRisk = Literal["low", "medium", "high"]
EvaluationReasonCode = Literal[
    "none",
    "wrong_tool",
    "unnecessary_tool",
    "ungrounded_arguments",
    "duplicate_action",
    "unsafe_side_effect",
    "incomplete_request",
    "other",
]
EvaluationMode = Literal["shadow", "enforce"]


@dataclass(frozen=True, slots=True)
class AgentStepEvaluationRequest:
    """Provider-neutral evidence used to assess one proposed tool-call batch."""

    role: AgentRole
    context: tuple[ConversationItem, ...]
    available_tools: tuple[ToolSpec, ...]
    proposed_calls: tuple[ToolCall, ...]

    def __post_init__(self) -> None:
        if not self.proposed_calls:
            raise ValueError("An evaluation requires at least one proposed tool call")


@dataclass(frozen=True, slots=True)
class AgentStepEvaluation:
    """Structured judgment produced before a proposed tool-call batch executes."""

    verdict: EvaluationVerdict
    risk: EvaluationRisk
    reason_code: EvaluationReasonCode
    feedback: str
    model: str
    usage: ModelUsage = ModelUsage()

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Evaluation model cannot be empty")
        if self.verdict == "pass" and self.reason_code != "none":
            raise ValueError("Passing evaluations must use the none reason code")
        if self.verdict != "pass" and self.reason_code == "none":
            raise ValueError("Rejected evaluations must explain their reason code")
        if self.verdict != "pass" and not self.feedback.strip():
            raise ValueError("Rejected evaluations must include actionable feedback")

    @property
    def passed(self) -> bool:
        """Return whether policy may execute the proposed batch."""
        return self.verdict == "pass"


@dataclass(frozen=True, slots=True)
class EvaluationTrace:
    """Persisted audit record for one evaluated tool-call proposal."""

    trace_id: str
    conversation_id: str
    turn_id: str
    job_id: str | None
    attempt: int
    proposed_call_ids: tuple[str, ...]
    verdict: EvaluationVerdict
    risk: EvaluationRisk
    reason_code: EvaluationReasonCode
    feedback: str
    mode: EvaluationMode
    executed: bool
    model: str
    usage: ModelUsage
    latency_ms: float
    created_at: datetime

    def __post_init__(self) -> None:
        identifiers = (self.trace_id, self.conversation_id, self.turn_id)
        if any(not value.strip() for value in identifiers):
            raise ValueError("Evaluation trace identifiers cannot be empty")
        if self.job_id is not None and not self.job_id.strip():
            raise ValueError("Evaluation trace job ID cannot be empty")
        if self.attempt < 1:
            raise ValueError("Evaluation trace attempt must be positive")
        if not self.proposed_call_ids or any(not value.strip() for value in self.proposed_call_ids):
            raise ValueError("Evaluation trace requires proposed call IDs")
        if len(set(self.proposed_call_ids)) != len(self.proposed_call_ids):
            raise ValueError("Evaluation trace call IDs must be unique")
        if not self.model.strip():
            raise ValueError("Evaluation trace model cannot be empty")
        if self.latency_ms < 0:
            raise ValueError("Evaluation trace latency cannot be negative")


@dataclass(frozen=True, slots=True)
class EvaluationTracePage:
    """Bounded audit projection returned with technical conversation history."""

    traces: tuple[EvaluationTrace, ...] = ()
    truncated: bool = False
