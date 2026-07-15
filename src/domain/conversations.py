from dataclasses import dataclass
from typing import Literal, TypeAlias

from domain.tools import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """Stable ownership context used to address one conversation safely."""

    conversation_id: str
    user_id: str
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """Provider-neutral message retained between agent executions."""

    role: Literal["user", "assistant"]
    content: str


ConversationItem: TypeAlias = ConversationMessage | ToolCall | ToolResult


@dataclass(frozen=True, slots=True)
class Conversation:
    """Versioned conversation aggregate containing provider-neutral history items."""

    key: ConversationKey
    messages: tuple[ConversationItem, ...] = ()
    version: int = 0
    title: str | None = None
