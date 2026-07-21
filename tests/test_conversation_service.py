import uuid

import pytest

from application.conversations import ConversationNotFoundError, ConversationService
from domain.conversations import Conversation, ConversationItem, ConversationKey


class StubConversationRepository:
    """Persist conversations in memory for lifecycle use-case tests."""

    def __init__(self) -> None:
        """Initialize empty canonical storage."""
        self.values: dict[str, Conversation] = {}

    async def create(self, key: ConversationKey) -> Conversation:
        """Create an empty conversation."""
        conversation = Conversation(key=key, title="Nueva conversación")
        self.values[key.conversation_id] = conversation
        return conversation

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Load one conversation by UID."""
        return self.values.get(key.conversation_id)

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Declare the complete repository contract; unused in these tests."""
        raise NotImplementedError

    async def delete(self, key: ConversationKey) -> bool:
        """Delete one conversation by UID."""
        return self.values.pop(key.conversation_id, None) is not None


async def test_conversation_service_owns_session_uid_creation_and_validation() -> None:
    """Keep session lifecycle outside model orchestration."""
    repository = StubConversationRepository()
    fixed_uid = uuid.UUID("12345678-1234-4678-9234-567812345678")
    service = ConversationService(repository, uid_factory=lambda: fixed_uid)

    created = await service.create_session("user-1")

    assert created.key == ConversationKey(conversation_id=str(fixed_uid), user_id="user-1")
    assert await service.require(created.key) == created
    assert await service.delete(created.key) is True
    with pytest.raises(ConversationNotFoundError):
        await service.require(created.key)
