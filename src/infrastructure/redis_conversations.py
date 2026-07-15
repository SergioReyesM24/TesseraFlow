import hashlib
import json
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis

from application.conversations import ConversationAccessDeniedError, ConversationTooLargeError
from domain.conversations import Conversation, ConversationItem, ConversationKey
from infrastructure.conversation_codec import (
    decode_conversation_item,
    encode_conversation_item,
)


class InvalidConversationDataError(RuntimeError):
    """Raised when cached Redis data violates the neutral conversation schema."""


class RedisConversationCache:
    """TTL cache for compacted model context; PostgreSQL remains authoritative."""

    def __init__(self, client: Redis, *, ttl_seconds: int, max_bytes: int) -> None:
        """Bind a shared Redis client and explicit cache retention limits."""
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._max_bytes = max_bytes

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Load cached context while enforcing the cached ownership boundary."""
        values = await cast(
            Awaitable[dict[object, object]],
            self._client.hgetall(self._storage_key(key.conversation_id)),
        )
        if not values:
            return None
        normalized = {self._text(name): self._text(value) for name, value in values.items()}
        if normalized.get("user_id") != key.user_id or normalized.get("tenant_id") != (
            key.tenant_id or ""
        ):
            raise ConversationAccessDeniedError("Conversation ownership does not match")
        try:
            messages = self._decode_messages(normalized["messages"])
            version = int(normalized["version"])
            title = normalized.get("title") or None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidConversationDataError("Cached conversation data is invalid") from exc
        if version < 1:
            raise InvalidConversationDataError("Cached conversation version is invalid")
        return Conversation(key=key, messages=messages, version=version, title=title)

    async def store(self, conversation: Conversation) -> None:
        """Replace cached context after checking its serialized byte limit."""
        payload = json.dumps(
            [encode_conversation_item(item) for item in conversation.messages],
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > self._max_bytes:
            raise ConversationTooLargeError(
                "Conversation exceeds the configured serialized cache limit"
            )
        storage_key = self._storage_key(conversation.key.conversation_id)
        async with self._client.pipeline(transaction=True) as pipeline:
            pipeline.hset(
                storage_key,
                mapping={
                    "user_id": conversation.key.user_id,
                    "tenant_id": conversation.key.tenant_id or "",
                    "version": conversation.version,
                    "title": conversation.title or "",
                    "messages": payload,
                },
            )
            pipeline.expire(storage_key, self._ttl_seconds)
            await pipeline.execute()

    async def invalidate(self, key: ConversationKey) -> None:
        """Remove cached context without affecting canonical conversation data."""
        await self._client.delete(self._storage_key(key.conversation_id))

    @staticmethod
    def _storage_key(conversation_id: str) -> str:
        """Hash public IDs so Redis keys do not expose user-provided identifiers."""
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return f"conversation:context:v2:{digest}"

    @staticmethod
    def _text(value: object) -> str:
        """Normalize Redis clients configured with or without response decoding."""
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    @staticmethod
    def _decode_messages(payload: str) -> tuple[ConversationItem, ...]:
        """Validate cached JSON before rebuilding neutral history items."""
        raw_messages = json.loads(payload)
        if not isinstance(raw_messages, list):
            raise ValueError("messages must be a list")
        return tuple(decode_conversation_item(item) for item in raw_messages)
