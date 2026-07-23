import json
import uuid
from collections.abc import Callable

from application.ports import ConversationHistoryRepository, ConversationRepository
from domain.conversations import (
    Conversation,
    ConversationGroup,
    ConversationHistoryPage,
    ConversationItem,
    ConversationKey,
    ConversationListPage,
    ConversationMessage,
)
from domain.tools import ToolCall


class ConversationAccessDeniedError(PermissionError):
    """Raised when a conversation belongs to another security principal."""


class ConversationConflictError(RuntimeError):
    """Raised when another request updated a conversation concurrently."""


class ConversationNotFoundError(LookupError):
    """Raised when a requested conversation has not been created."""


class ConversationTooLargeError(ValueError):
    """Raised when a conversation cannot fit within configured storage limits."""


class ConversationService:
    """Manage persisted conversation lifecycle independently from model orchestration."""

    def __init__(
        self,
        conversations: ConversationRepository,
        *,
        uid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        """Bind persistence and an injectable UID generator."""
        self._conversations = conversations
        self._uid_factory = uid_factory

    async def create_session(self, user_id: str) -> Conversation:
        """Create an empty owned conversation with a server-generated session UID."""
        key = ConversationKey(
            conversation_id=str(self._uid_factory()),
            user_id=user_id,
        )
        return await self._conversations.create(key)

    async def require(self, key: ConversationKey) -> Conversation:
        """Return an existing owned conversation or reject an unknown session UID."""
        conversation = await self._conversations.load(key)
        if conversation is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        return conversation

    async def delete(self, key: ConversationKey) -> bool:
        """Delete an owned conversation and all retained history."""
        return await self._conversations.delete(key)


class ConversationHistoryService:
    """Expose bounded canonical history without coupling API code to PostgreSQL."""

    def __init__(self, histories: ConversationHistoryRepository) -> None:
        """Bind the owner-aware source of canonical conversation records."""
        self._histories = histories

    async def list_sessions(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ConversationListPage:
        """Return one bounded page of sessions owned by a user."""
        return await self._histories.list_sessions(
            user_id,
            offset=offset,
            limit=limit,
        )

    async def load(
        self,
        key: ConversationKey,
        *,
        after_sequence: int,
        limit: int,
    ) -> ConversationHistoryPage:
        """Return a technical history page or reject an unknown session."""
        history = await self._histories.load_history(
            key,
            after_sequence=after_sequence,
            limit=limit,
        )
        if history is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        return history

    async def load_group(self, key: ConversationKey) -> ConversationGroup:
        """Return the root projection containing the requested owned conversation."""
        group = await self._histories.load_group(key)
        if group is None:
            raise ConversationNotFoundError("Conversation session does not exist")
        return group


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
