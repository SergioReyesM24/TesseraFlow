import structlog

from application.conversations import RecentConversationCompactor
from application.ports import ConversationCache, ConversationRepository
from domain.conversations import Conversation, ConversationItem, ConversationKey

logger = structlog.get_logger(__name__)


class CachedConversationRepository(ConversationRepository):
    """Coordinate canonical persistence with a best-effort compacted context cache."""

    def __init__(
        self,
        canonical: ConversationRepository,
        cache: ConversationCache,
        compactor: RecentConversationCompactor,
    ) -> None:
        """Bind the canonical repository, disposable cache, and context policy."""
        self._canonical = canonical
        self._cache = cache
        self._compactor = compactor

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Read through Redis and rebuild its entry from PostgreSQL after a miss."""
        try:
            cached = await self._cache.load(key)
        except Exception as exc:
            logger.warning("conversation_cache_load_failed", error_type=type(exc).__name__)
        else:
            if cached is not None:
                return cached

        canonical = await self._canonical.load(key)
        if canonical is None:
            return None
        compacted = self._compact(canonical)
        await self._store_best_effort(compacted)
        return compacted

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Commit to PostgreSQL first, then refresh Redis without risking data loss."""
        saved = await self._canonical.save_turn(conversation, turn)
        compacted = self._compact(saved)
        await self._store_best_effort(compacted)
        return compacted

    async def delete(self, key: ConversationKey) -> bool:
        """Delete canonical data before invalidating the disposable cache entry."""
        deleted = await self._canonical.delete(key)
        try:
            await self._cache.invalidate(key)
        except Exception as exc:
            logger.warning("conversation_cache_invalidate_failed", error_type=type(exc).__name__)
        return deleted

    def _compact(self, conversation: Conversation) -> Conversation:
        """Apply the model-context window while retaining canonical metadata/version."""
        return Conversation(
            key=conversation.key,
            messages=self._compactor.compact(conversation.messages),
            version=conversation.version,
            title=conversation.title,
        )

    async def _store_best_effort(self, conversation: Conversation) -> None:
        """Keep cache failures outside the canonical persistence path."""
        try:
            await self._cache.store(conversation)
        except Exception as exc:
            logger.warning("conversation_cache_store_failed", error_type=type(exc).__name__)
