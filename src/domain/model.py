from dataclasses import dataclass

from domain.costs import ModelUsage
from domain.tools import ToolCall


@dataclass(frozen=True, slots=True)
class ModelReply:
    """Normalized reply produced by any model provider."""

    response_id: str
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ModelUsage = ModelUsage()
