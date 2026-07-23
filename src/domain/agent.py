from dataclasses import dataclass, field

from domain.tools import ToolCallRecord
from domain.visuals import VisualPresentation


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Immutable configuration selected for one agent execution."""

    model: str
    instructions: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Final provider-neutral outcome returned by the application service."""

    answer: str
    response_id: str
    conversation_id: str
    tool_calls: tuple[ToolCallRecord, ...] = field(default_factory=tuple)
    visual_components: tuple[VisualPresentation, ...] = field(default_factory=tuple)
