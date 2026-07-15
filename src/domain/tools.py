from dataclasses import dataclass
from typing import Any, Literal

from domain.types import JsonObject


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Provider-neutral description of a tool exposed to a model."""

    name: str
    description: str
    arguments_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider-neutral request from a model to execute a tool."""

    call_id: str
    tool_name: str
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Provider-neutral result returned to the model after a tool call."""

    call_id: str
    output: Any | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """Auditable record of one local tool execution exposed to API callers."""

    call_id: str
    tool_name: str
    arguments: JsonObject
    status: Literal["success", "error"]
    output: Any | None
    error: str | None
    duration_ms: float
