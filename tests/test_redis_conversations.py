import pytest

from application.conversations import (
    ConversationAccessDeniedError,
    ConversationTooLargeError,
    RecentConversationCompactor,
)
from domain.conversations import Conversation, ConversationKey, ConversationMessage
from domain.tools import ToolCall, ToolResult
from infrastructure.redis_conversations import RedisConversationCache


class FakeRedis:
    """Minimal Redis behavior needed to exercise the disposable context cache."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

    async def hgetall(self, key: str) -> dict[str, str]:
        """Return one cached hash or an empty mapping."""
        return self.values.get(key, {})

    async def delete(self, key: str) -> int:
        """Remove one cached hash."""
        return int(self.values.pop(key, None) is not None)

    async def eval(self, script: str, key_count: int, *args: object) -> int:
        """Apply the version-aware cache replacement represented by the Lua script."""
        assert key_count == 1
        assert "current_version" in script
        key, version, user_id, title, messages, ttl = args
        assert isinstance(key, str)
        current = self.values.get(key)
        if current is not None and int(current["version"]) > int(str(version)):
            return 0
        self.values[key] = {
            "version": str(version),
            "user_id": str(user_id),
            "title": str(title),
            "messages": str(messages),
        }
        self.expirations[key] = int(str(ttl))
        return 1


def conversation(*, version: int = 1, user_id: str = "user-1") -> Conversation:
    """Build one compacted conversation containing a complete tool turn."""
    return Conversation(
        key=ConversationKey(conversation_id="conversation-1", user_id=user_id),
        messages=(
            ConversationMessage(role="user", content="Suma"),
            ToolCall(call_id="call_1", tool_name="calculator", arguments={"a": 2, "b": 3}),
            ToolResult(call_id="call_1", output={"result": 5}),
            ConversationMessage(role="assistant", content="5"),
        ),
        version=version,
        title="Suma",
    )


async def test_redis_cache_round_trip_ttl_and_invalidation() -> None:
    """Cache complete structured context with a TTL and allow disposable removal."""
    client = FakeRedis()
    cache = RedisConversationCache(client, ttl_seconds=3600, max_bytes=10_000)

    await cache.store(conversation())
    loaded = await cache.load(conversation().key)
    await cache.invalidate(conversation().key)

    assert loaded == conversation()
    assert set(client.expirations.values()) == {3600}
    assert await cache.load(conversation().key) is None


async def test_redis_cache_rejects_other_owners() -> None:
    """Do not expose cached context when the supplied owner differs."""
    cache = RedisConversationCache(FakeRedis(), ttl_seconds=60, max_bytes=10_000)
    await cache.store(conversation())

    with pytest.raises(ConversationAccessDeniedError):
        await cache.load(conversation(user_id="user-2").key)


async def test_redis_cache_enforces_serialized_size_limit() -> None:
    """Reject cache payloads that exceed the explicit byte budget."""
    cache = RedisConversationCache(FakeRedis(), ttl_seconds=60, max_bytes=10)

    with pytest.raises(ConversationTooLargeError):
        await cache.store(conversation())


async def test_redis_cache_does_not_replace_newer_context_with_stale_data() -> None:
    """Use the cached version to reject an out-of-order refresh from another request."""
    cache = RedisConversationCache(FakeRedis(), ttl_seconds=60, max_bytes=10_000)
    newer = conversation(version=2)
    stale = Conversation(
        key=newer.key,
        messages=(
            ConversationMessage(role="user", content="Viejo"),
            ConversationMessage(role="assistant", content="Viejo"),
        ),
        version=1,
    )

    await cache.store(newer)
    await cache.store(stale)

    assert await cache.load(newer.key) == newer


def test_compactor_keeps_complete_newest_turn_with_tools() -> None:
    """Never retain a tool call without the rest of its turn."""
    compactor = RecentConversationCompactor(max_messages=4, max_characters=1_000)
    messages = (
        ConversationMessage(role="user", content="old"),
        ConversationMessage(role="assistant", content="1234"),
        *conversation().messages,
    )

    assert compactor.compact(messages) == conversation().messages
