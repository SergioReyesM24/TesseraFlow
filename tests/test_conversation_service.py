import uuid
from datetime import UTC, datetime

import pytest

from application.conversations import (
    ConversationHistoryService,
    ConversationNotFoundError,
    ConversationService,
)
from domain.conversations import (
    Conversation,
    ConversationCorrelation,
    ConversationGroup,
    ConversationGroupMember,
    ConversationHistoryPage,
    ConversationItem,
    ConversationKey,
    ConversationListPage,
    ConversationSummary,
)


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
        *,
        turn_id: str,
    ) -> Conversation:
        """Declare the complete repository contract; unused in these tests."""
        del conversation, turn, turn_id
        raise NotImplementedError

    async def delete(self, key: ConversationKey) -> bool:
        """Delete one conversation by UID."""
        return self.values.pop(key.conversation_id, None) is not None


class StubConversationHistoryRepository:
    """Return one owner-scoped technical page for history service tests."""

    async def list_sessions(
        self,
        user_id: str,
        *,
        offset: int,
        limit: int,
    ) -> ConversationListPage:
        """Return one deterministic summary page for the requested owner."""
        assert offset == 0
        assert limit == 50
        timestamp = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        return ConversationListPage(
            sessions=(
                ConversationSummary(
                    key=ConversationKey(conversation_id="known", user_id=user_id),
                    title="Historial",
                    status="active",
                    version=0,
                    last_sequence=0,
                    created_at=timestamp,
                    updated_at=timestamp,
                    last_message_at=None,
                    correlation=ConversationCorrelation(
                        conversation_id="known",
                        root_conversation_id="known",
                    ),
                ),
            ),
            has_more=False,
        )

    async def load_history(
        self,
        key: ConversationKey,
        *,
        after_sequence: int,
        limit: int,
    ) -> ConversationHistoryPage | None:
        """Return deterministic metadata only for the known conversation."""
        assert after_sequence == 0
        assert limit == 50
        if key.conversation_id != "known":
            return None
        timestamp = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
        return ConversationHistoryPage(
            key=key,
            title="Historial",
            status="active",
            version=0,
            last_sequence=0,
            created_at=timestamp,
            updated_at=timestamp,
            last_message_at=None,
            items=(),
            has_more=False,
            correlation=ConversationCorrelation(
                conversation_id=key.conversation_id,
                root_conversation_id=key.conversation_id,
            ),
        )

    async def load_group(self, key: ConversationKey) -> ConversationGroup | None:
        """Return one root-only group for the known conversation."""
        if key.conversation_id != "known":
            return None
        correlation = ConversationCorrelation(
            conversation_id=key.conversation_id,
            root_conversation_id=key.conversation_id,
        )
        return ConversationGroup(
            root_conversation=key,
            members=(ConversationGroupMember(correlation=correlation),),
        )


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


async def test_history_service_rejects_unknown_sessions() -> None:
    """Keep not-found semantics in the application boundary."""
    service = ConversationHistoryService(StubConversationHistoryRepository())
    known = ConversationKey(conversation_id="known", user_id="user-1")

    listed = await service.list_sessions("user-1", offset=0, limit=50)
    assert listed.sessions[0].key == known
    assert (await service.load(known, after_sequence=0, limit=50)).title == "Historial"
    assert (await service.load_group(known)).root_conversation == known
    with pytest.raises(ConversationNotFoundError):
        await service.load(
            ConversationKey(conversation_id="missing", user_id="user-1"),
            after_sequence=0,
            limit=50,
        )
