from dataclasses import dataclass

import asyncpg
import httpx
from openai import AsyncOpenAI
from redis.asyncio import Redis

from application.agent import AgentService
from application.conversations import ConversationService, RecentConversationCompactor
from config import Settings
from domain.agent import AgentDefinition
from infrastructure.cached_conversations import CachedConversationRepository
from infrastructure.openai_gateway import OpenAIResponsesGateway
from infrastructure.postgres_conversations import (
    PostgresConversationRepository,
    apply_postgres_migrations,
)
from infrastructure.redis_conversations import RedisConversationCache
from tools.registry import build_tool_registry


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Application-wide resources and default immutable configuration."""

    openai_client: AsyncOpenAI
    redis_client: Redis
    postgres_pool: asyncpg.Pool
    agent_service: AgentService
    conversation_service: ConversationService
    default_agent: AgentDefinition

    async def close(self) -> None:
        """Release application-wide clients during graceful shutdown."""
        await self.openai_client.close()
        await self.redis_client.aclose()
        await self.postgres_pool.close()


async def build_container(settings: Settings) -> AppContainer:
    """Compose concrete adapters, application services, and default configuration."""
    if settings.postgres_pool_min_size > settings.postgres_pool_max_size:
        raise ValueError("POSTGRES_POOL_MIN_SIZE cannot exceed POSTGRES_POOL_MAX_SIZE")
    postgres_pool = await asyncpg.create_pool(
        dsn=settings.postgres_url,
        min_size=settings.postgres_pool_min_size,
        max_size=settings.postgres_pool_max_size,
        command_timeout=settings.postgres_command_timeout_seconds,
    )
    if postgres_pool is None:
        raise RuntimeError("asyncpg did not create a PostgreSQL pool")
    try:
        await apply_postgres_migrations(postgres_pool)
    except BaseException:
        await postgres_pool.close()
        raise
    client = AsyncOpenAI(
        api_key=settings.openai_api_key or "missing-api-key",
        base_url=settings.openai_base_url,
        timeout=httpx.Timeout(
            connect=settings.openai_connect_timeout_seconds,
            read=600.0,
            write=600.0,
            pool=600.0,
        ),
    )
    tools = build_tool_registry()
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    definition = AgentDefinition(
        model=settings.openai_model,
        instructions=settings.agent_instructions,
        tool_names=tools.names,
    )
    conversations = CachedConversationRepository(
        canonical=PostgresConversationRepository(
            postgres_pool,
            context_item_limit=settings.conversation_max_messages,
        ),
        cache=RedisConversationCache(
            redis_client,
            ttl_seconds=settings.conversation_ttl_seconds,
            max_bytes=settings.conversation_max_bytes,
        ),
        compactor=RecentConversationCompactor(
            max_messages=settings.conversation_max_messages,
            max_characters=settings.conversation_max_characters,
        ),
    )
    agent_service = AgentService(
        model_gateway=OpenAIResponsesGateway(client),
        tools=tools,
        conversations=conversations,
        max_tool_rounds=settings.max_tool_rounds,
    )
    return AppContainer(
        openai_client=client,
        redis_client=redis_client,
        postgres_pool=postgres_pool,
        agent_service=agent_service,
        conversation_service=ConversationService(conversations),
        default_agent=definition,
    )
