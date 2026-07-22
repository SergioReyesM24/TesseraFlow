from dataclasses import dataclass
from uuid import uuid4

import asyncpg
from redis.asyncio import Redis

from application.a2a import A2AService, A2AWorker
from application.agent import AgentService
from application.conversations import ConversationService, RecentConversationCompactor
from application.interactions import ConversationCoordinator
from application.ports import InteractionNotifier
from application.realtime import RealtimeAgentService
from config import Settings
from domain.agent import AgentDefinition
from infrastructure.cached_conversations import CachedConversationRepository
from infrastructure.model_runtime import ModelRuntime, build_model_runtime
from infrastructure.postgres_a2a import PostgresA2AJobRepository
from infrastructure.postgres_conversations import (
    PostgresConversationRepository,
    apply_postgres_migrations,
)
from infrastructure.postgres_interaction_notifier import PostgresInteractionNotifier
from infrastructure.postgres_interactions import PostgresInteractionRepository
from infrastructure.redis_conversations import RedisConversationCache
from tools.registry import build_interactive_tool_registry, build_tool_registry


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Application-wide resources and default immutable configuration."""

    model_runtime: ModelRuntime
    redis_client: Redis
    postgres_pool: asyncpg.Pool
    agent_service: AgentService
    conversation_service: ConversationService
    default_agent: AgentDefinition
    realtime_agent_service: RealtimeAgentService | None
    a2a_service: A2AService
    a2a_worker: A2AWorker
    interaction_notifier: InteractionNotifier
    conversation_coordinator: ConversationCoordinator

    async def start(self) -> None:
        """Start process-local background consumers after lifespan composition."""
        await self.interaction_notifier.start()
        self.conversation_coordinator.start()
        self.a2a_worker.start()

    async def close(self) -> None:
        """Release application-wide clients during graceful shutdown."""
        await self.a2a_worker.close()
        await self.conversation_coordinator.close()
        await self.interaction_notifier.close()
        await self.model_runtime.close()
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
    redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
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
    jobs = PostgresA2AJobRepository(postgres_pool)
    interactions = PostgresInteractionRepository(
        postgres_pool,
        max_pending_commands=settings.interaction_max_pending_commands,
    )
    interaction_notifier = PostgresInteractionNotifier(
        settings.postgres_url,
        command_timeout_seconds=settings.postgres_command_timeout_seconds,
    )
    a2a_service = A2AService(jobs, conversations)
    worker_tools = build_tool_registry()
    interactive_tools = build_interactive_tool_registry(a2a_service)
    model_runtime = build_model_runtime(
        settings,
        conversations=conversations,
        interactive_tools=interactive_tools,
        worker_tools=worker_tools,
    )
    a2a_worker = A2AWorker(
        jobs,
        interaction_notifier,
        model_runtime.worker_agent_service,
        conversations,
        model_runtime.worker_definition,
        worker_id=str(uuid4()),
        reconciliation_seconds=settings.a2a_worker_reconciliation_seconds,
        job_timeout_seconds=settings.a2a_job_timeout_seconds,
    )
    conversation_coordinator = ConversationCoordinator(
        interactions,
        interaction_notifier,
        model_runtime.interactive_agent,
        worker_id=str(uuid4()),
        reconciliation_seconds=settings.interaction_coordinator_reconciliation_seconds,
        output_reconciliation_seconds=settings.interaction_output_reconciliation_seconds,
        command_timeout_seconds=settings.interaction_command_timeout_seconds,
        worker_count=settings.interaction_coordinator_workers,
    )
    return AppContainer(
        model_runtime=model_runtime,
        redis_client=redis_client,
        postgres_pool=postgres_pool,
        agent_service=model_runtime.agent_service,
        conversation_service=ConversationService(conversations),
        default_agent=model_runtime.default_agent,
        realtime_agent_service=model_runtime.realtime_agent_service,
        a2a_service=a2a_service,
        a2a_worker=a2a_worker,
        interaction_notifier=interaction_notifier,
        conversation_coordinator=conversation_coordinator,
    )
