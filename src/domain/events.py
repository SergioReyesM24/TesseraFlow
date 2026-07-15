from dataclasses import dataclass
from typing import TypeAlias

from domain.agent import AgentResult
from domain.model import ModelReply
from domain.tools import ToolCallRecord


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """Provider-neutral text fragment emitted while a model is responding."""

    text: str


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    """Terminal model-stream event containing the fully accumulated reply."""

    reply: ModelReply


ModelStreamEvent: TypeAlias = ModelTextDelta | ModelStreamCompleted


@dataclass(frozen=True, slots=True)
class AgentTextDelta:
    """Text fragment ready to be forwarded to an agent-stream consumer."""

    text: str


@dataclass(frozen=True, slots=True)
class AgentToolStarted:
    """Notification that the agent is about to execute a requested tool."""

    call_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class AgentToolCompleted:
    """Notification containing the auditable result of a tool execution."""

    record: ToolCallRecord


@dataclass(frozen=True, slots=True)
class AgentStreamCompleted:
    """Terminal agent-stream event containing the complete application result."""

    result: AgentResult


AgentStreamEvent: TypeAlias = (
    AgentTextDelta | AgentToolStarted | AgentToolCompleted | AgentStreamCompleted
)
