import json

from domain.conversations import ConversationItem, ConversationMessage
from domain.tools import ToolCall


class ConversationAccessDeniedError(PermissionError):
    """Raised when a conversation belongs to another security principal."""


class ConversationConflictError(RuntimeError):
    """Raised when another request updated a conversation concurrently."""


class ConversationTooLargeError(ValueError):
    """Raised when a conversation cannot fit within configured storage limits."""


class RecentConversationCompactor:
    """Keep the newest complete turns within deterministic item and character limits."""

    def __init__(self, *, max_messages: int, max_characters: int) -> None:
        """Configure hard bounds applied before each persistent write."""
        if max_messages < 2:
            raise ValueError("max_messages must be at least 2")
        if max_characters < 2:
            raise ValueError("max_characters must be at least 2")
        self._max_messages = max_messages
        self._max_characters = max_characters

    def compact(
        self,
        messages: tuple[ConversationItem, ...],
    ) -> tuple[ConversationItem, ...]:
        """Drop oldest complete turns without separating tool calls from their results."""
        turns = self._split_turns(messages)
        kept: list[tuple[ConversationItem, ...]] = []
        characters = 0
        for turn in reversed(turns):
            required = sum(self._character_count(item) for item in turn)
            kept_count = sum(len(item) for item in kept)
            if not kept and len(turn) > self._max_messages:
                raise ConversationTooLargeError(
                    "The latest conversation turn exceeds the configured item limit"
                )
            if kept_count + len(turn) > self._max_messages:
                break
            if kept and characters + required > self._max_characters:
                break
            if not kept and required > self._max_characters:
                raise ConversationTooLargeError(
                    "The latest conversation turn exceeds the configured character limit"
                )
            kept.append(turn)
            characters += required
        return tuple(item for turn in reversed(kept) for item in turn)

    @staticmethod
    def _split_turns(
        messages: tuple[ConversationItem, ...],
    ) -> tuple[tuple[ConversationItem, ...], ...]:
        """Split history at user messages and reject structurally incomplete turns."""
        if not messages:
            return ()
        starts = [
            index
            for index, item in enumerate(messages)
            if isinstance(item, ConversationMessage) and item.role == "user"
        ]
        if not starts or starts[0] != 0:
            raise ValueError("Conversation history must start with a user message")
        turns: list[tuple[ConversationItem, ...]] = []
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(messages)
            turn = messages[start:end]
            last = turn[-1]
            if not isinstance(last, ConversationMessage) or last.role != "assistant":
                raise ValueError("Every persisted conversation turn must end with an assistant")
            turns.append(turn)
        return tuple(turns)

    @staticmethod
    def _character_count(item: ConversationItem) -> int:
        """Count retained text plus serialized tool arguments and results."""
        if isinstance(item, ConversationMessage):
            return len(item.content)
        payload: dict[str, object]
        if isinstance(item, ToolCall):
            payload = {
                "call_id": item.call_id,
                "tool_name": item.tool_name,
                "arguments": item.arguments,
            }
        else:
            payload = {"call_id": item.call_id, "output": item.output, "error": item.error}
        return len(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))
