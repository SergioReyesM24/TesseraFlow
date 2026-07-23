import pytest

from application.conversations import ConversationTooLargeError, RecentConversationCompactor
from domain.conversations import (
    Conversation,
    ConversationItem,
    ConversationKey,
    ConversationMessage,
)
from infrastructure.cached_conversations import CachedConversationRepository


class StubCanonicalRepository:
    """Record canonical operations while behaving like append-only storage."""

    def __init__(self, value: Conversation | None = None) -> None:
        """Initialize the canonical value and operation counters."""
        self.value = value
        self.loads = 0
        self.saved_turns: list[tuple[ConversationItem, ...]] = []
        self.deletes = 0

    async def create(self, key: ConversationKey) -> Conversation:
        """Create and retain one empty canonical conversation."""
        self.value = Conversation(key=key, title="Nueva conversación")
        return self.value

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Return the configured canonical conversation."""
        self.loads += 1
        return self.value

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
        *,
        turn_id: str,
    ) -> Conversation:
        """Append one turn and increment the canonical version."""
        del turn_id
        self.saved_turns.append(turn)
        self.value = Conversation(
            key=conversation.key,
            messages=conversation.messages + turn,
            version=conversation.version + 1,
            title=conversation.title or "Title",
        )
        return self.value

    async def delete(self, key: ConversationKey) -> bool:
        """Delete the configured canonical conversation."""
        self.deletes += 1
        existed = self.value is not None
        self.value = None
        return existed


class StubConversationCache:
    """Expose cache hits, stores, and invalidations for coordinator tests."""

    def __init__(self, value: Conversation | None = None) -> None:
        """Initialize a possible cache hit."""
        self.value = value
        self.stores: list[Conversation] = []
        self.invalidations: list[ConversationKey] = []

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Return the current cached value."""
        return self.value

    async def store(self, conversation: Conversation) -> None:
        """Record and retain a cache refresh."""
        self.value = conversation
        self.stores.append(conversation)

    async def invalidate(self, key: ConversationKey) -> None:
        """Record and apply cache invalidation."""
        self.value = None
        self.invalidations.append(key)


class FailingConversationCache(StubConversationCache):
    """Simulate Redis being unavailable for every cache operation."""

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Fail cache reads like an unavailable Redis connection."""
        raise ConnectionError("redis unavailable")

    async def store(self, conversation: Conversation) -> None:
        """Fail cache writes like an unavailable Redis connection."""
        raise ConnectionError("redis unavailable")

    async def invalidate(self, key: ConversationKey) -> None:
        """Fail invalidation like an unavailable Redis connection."""
        raise ConnectionError("redis unavailable")


def key() -> ConversationKey:
    """Return one stable owned conversation key."""
    return ConversationKey(conversation_id="conv-1", user_id="user-1")


def turns() -> tuple[ConversationItem, ...]:
    """Return two complete text-only turns."""
    return (
        ConversationMessage(role="user", content="old"),
        ConversationMessage(role="assistant", content="old answer"),
        ConversationMessage(role="user", content="new"),
        ConversationMessage(role="assistant", content="new answer"),
    )


def coordinator(
    canonical: StubCanonicalRepository,
    cache: StubConversationCache,
) -> CachedConversationRepository:
    """Build a coordinator retaining only the newest two-item turn."""
    return CachedConversationRepository(
        canonical,
        cache,
        RecentConversationCompactor(max_messages=2, max_characters=1_000),
    )


async def test_cache_hit_avoids_canonical_read() -> None:
    """Serve active context directly from Redis when present."""
    cached = Conversation(key=key(), messages=turns()[-2:], version=2, title="Title")
    canonical = StubCanonicalRepository()

    loaded = await coordinator(canonical, StubConversationCache(cached)).load(key())

    assert loaded == cached
    assert canonical.loads == 0


async def test_create_persists_and_caches_an_empty_conversation() -> None:
    """Make a new session immediately visible through canonical and cache storage."""
    canonical = StubCanonicalRepository()
    cache = StubConversationCache()

    created = await coordinator(canonical, cache).create(key())

    assert created == Conversation(key=key(), title="Nueva conversación")
    assert canonical.value == created
    assert cache.value == created


async def test_cache_miss_rebuilds_compacted_context_from_canonical() -> None:
    """Load PostgreSQL after expiry and repopulate Redis with a bounded window."""
    full = Conversation(key=key(), messages=turns(), version=2, title="Title")
    canonical = StubCanonicalRepository(full)
    cache = StubConversationCache()

    loaded = await coordinator(canonical, cache).load(key())

    assert loaded == Conversation(key=key(), messages=turns()[-2:], version=2, title="Title")
    assert cache.stores == [loaded]


async def test_save_commits_canonical_turn_before_refreshing_cache() -> None:
    """Preserve the new turn canonically and expose only compacted model context."""
    previous = Conversation(key=key(), messages=turns()[:2], version=1, title="Title")
    canonical = StubCanonicalRepository(previous)
    cache = StubConversationCache(previous)
    turn = turns()[-2:]

    saved = await coordinator(canonical, cache).save_turn(
        previous,
        turn,
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert canonical.saved_turns == [turn]
    assert canonical.value is not None
    assert canonical.value.messages == turns()
    assert saved.messages == turn
    assert cache.value == saved


async def test_oversized_turn_is_rejected_before_canonical_write() -> None:
    """Avoid persisting a turn when it cannot fit in the configured model context."""
    canonical = StubCanonicalRepository()
    cache = StubConversationCache()
    repository = CachedConversationRepository(
        canonical,
        cache,
        RecentConversationCompactor(max_messages=2, max_characters=5),
    )
    turn = (
        ConversationMessage(role="user", content="demasiado largo"),
        ConversationMessage(role="assistant", content="respuesta"),
    )

    with pytest.raises(ConversationTooLargeError):
        await repository.save_turn(
            Conversation(key=key()),
            turn,
            turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        )

    assert canonical.saved_turns == []
    assert cache.stores == []


async def test_delete_removes_canonical_data_and_invalidates_cache() -> None:
    """Delete PostgreSQL history and its disposable Redis context."""
    value = Conversation(key=key(), messages=turns(), version=2)
    canonical = StubCanonicalRepository(value)
    cache = StubConversationCache(value)

    deleted = await coordinator(canonical, cache).delete(key())

    assert deleted is True
    assert canonical.value is None
    assert cache.invalidations == [key()]


async def test_redis_outage_does_not_block_canonical_load_save_or_delete() -> None:
    """Keep PostgreSQL authoritative and usable while the cache is unavailable."""
    previous = Conversation(key=key(), messages=turns()[:2], version=1, title="Title")
    canonical = StubCanonicalRepository(previous)
    repository = coordinator(canonical, FailingConversationCache())

    loaded = await repository.load(key())
    saved = await repository.save_turn(
        loaded or previous,
        turns()[-2:],
        turn_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    deleted = await repository.delete(key())

    assert loaded == previous
    assert saved.version == 2
    assert deleted is True
    assert canonical.value is None
