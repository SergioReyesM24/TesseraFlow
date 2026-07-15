from typing import Any

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

    def pipeline(self, *, transaction: bool) -> "FakePipeline":
        """Create a transaction-like command buffer."""
        assert transaction is True
        return FakePipeline(self)


class FakePipeline:
    """Execute queued cache writes when the transaction is committed."""

    def __init__(self, client: FakeRedis) -> None:
        """Bind the fake client receiving queued commands."""
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "FakePipeline":
        """Enter the fake transactional context."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Leave the fake transactional context."""
        return None

    def hset(self, key: str, *, mapping: dict[str, object]) -> "FakePipeline":
        """Queue a hash replacement."""
        self.commands.append(("hset", (key, mapping)))
        return self

    def expire(self, key: str, ttl: int) -> "FakePipeline":
        """Queue a TTL update."""
        self.commands.append(("expire", (key, ttl)))
        return self

    async def execute(self) -> list[object]:
        """Apply all queued commands in order."""
        results: list[object] = []
        for command, args in self.commands:
            key = str(args[0])
            if command == "hset":
                mapping = args[1]
                assert isinstance(mapping, dict)
                self.client.values[key] = {str(name): str(value) for name, value in mapping.items()}
                results.append(len(mapping))
            else:
                self.client.expirations[key] = int(args[1])
                results.append(True)
        return results


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


def test_compactor_keeps_complete_newest_turn_with_tools() -> None:
    """Never retain a tool call without the rest of its turn."""
    compactor = RecentConversationCompactor(max_messages=4, max_characters=1_000)
    messages = (
        ConversationMessage(role="user", content="old"),
        ConversationMessage(role="assistant", content="1234"),
        *conversation().messages,
    )

    assert compactor.compact(messages) == conversation().messages
