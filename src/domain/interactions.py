from dataclasses import dataclass
from typing import Literal

from domain.conversations import ConversationKey
from domain.events import AgentStreamEvent

InteractionCommandKind = Literal["user_message", "worker_completed"]
InteractionSource = Literal["text_user", "speech_user", "worker_agent"]
InteractionModality = Literal["text", "audio"]
InteractionCommandStatus = Literal["queued", "running", "completed", "failed"]


@dataclass(frozen=True, slots=True)
class InteractionCommand:
    """One durable input waiting to advance an interactive conversation."""

    command_id: str
    request_id: str
    conversation: ConversationKey
    kind: InteractionCommandKind
    source: InteractionSource
    message: str
    causation_id: str | None = None
    status: InteractionCommandStatus = "queued"
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class InteractionOutput:
    """One durable, correlated output emitted while processing a command."""

    output_id: str
    command_id: str
    request_id: str
    conversation: ConversationKey
    modality: InteractionModality
    event: AgentStreamEvent
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class InteractionEmission:
    """One live modality-tagged event produced by an interactive agent adapter."""

    modality: InteractionModality
    event: AgentStreamEvent


def is_terminal_output(output: InteractionOutput) -> bool:
    """Report whether an output closes its command's public event stream."""
    from domain.events import AgentStreamCompleted, AgentStreamFailed

    return isinstance(output.event, AgentStreamCompleted | AgentStreamFailed)
