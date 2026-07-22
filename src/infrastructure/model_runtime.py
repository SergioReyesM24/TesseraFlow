from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from google import genai
from openai import AsyncOpenAI

from application.agent import AgentService
from application.interactions import TurnInteractionAgent
from application.ports import (
    ConversationRepository,
    InteractionNotifier,
    InteractionRepository,
    InteractiveAgent,
    ModelGateway,
    RealtimeModelGateway,
)
from application.realtime import RealtimeAgentService
from application.tools import ToolRegistry
from config import Settings
from domain.agent import AgentDefinition
from infrastructure.gemini_realtime_gateway import GeminiRealtimeGateway
from infrastructure.openai_gateway import OpenAIResponsesGateway

AsyncCloser = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    """Provider-neutral role runtimes and their process-owned client lifecycle."""

    text_agent: InteractiveAgent
    text_agent_service: AgentService
    text_definition: AgentDefinition
    worker_agent_service: AgentService
    worker_definition: AgentDefinition
    realtime_agent_service: RealtimeAgentService
    realtime_definition: AgentDefinition
    text_agent_provider: str
    realtime_agent_provider: str
    worker_provider: str
    _closers: tuple[AsyncCloser, ...]

    async def close(self) -> None:
        """Close every provider client once in reverse construction order."""
        for close in reversed(self._closers):
            await close()


def build_model_runtime(
    settings: Settings,
    *,
    conversations: ConversationRepository,
    interactive_tools: ToolRegistry,
    worker_tools: ToolRegistry,
    interactions: InteractionRepository | None = None,
    interaction_notifier: InteractionNotifier | None = None,
) -> ModelRuntime:
    """Compose independent text, realtime, and worker provider roles."""
    _validate_selection(settings)
    closers: list[AsyncCloser] = []
    openai_gateway: OpenAIResponsesGateway | None = None
    gemini_client: genai.Client | None = None
    gemini_realtime_gateway: GeminiRealtimeGateway | None = None

    def get_openai_gateway() -> OpenAIResponsesGateway:
        """Create one shared OpenAI client lazily for text-compatible roles."""
        nonlocal openai_gateway
        if openai_gateway is None:
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
            closers.append(client.close)
            openai_gateway = OpenAIResponsesGateway(client)
        return openai_gateway

    def get_gemini_client() -> genai.Client:
        """Create the process-shared client used by registered Gemini adapters."""
        nonlocal gemini_client
        if gemini_client is None:
            gemini_client = genai.Client(
                api_key=settings.gemini_api_key or "missing-api-key",
                http_options=genai.types.HttpOptions(api_version=settings.gemini_live_api_version),
            )
            closers.append(gemini_client.aio.aclose)
        return gemini_client

    def get_gemini_realtime_gateway() -> GeminiRealtimeGateway:
        """Create the current STS adapter without exposing it to the core."""
        nonlocal gemini_realtime_gateway
        if gemini_realtime_gateway is None:
            gemini_realtime_gateway = GeminiRealtimeGateway(
                get_gemini_client(),
                model=settings.realtime_agent_model,
                voice_name=settings.gemini_live_voice_name,
                input_language_code=settings.gemini_live_language_code,
                max_resumption_attempts=settings.realtime_resumption_max_attempts,
                resumption_timeout_seconds=settings.realtime_resumption_timeout_seconds,
            )
        return gemini_realtime_gateway

    text_gateways: dict[str, Callable[[], ModelGateway]] = {
        "openai": get_openai_gateway,
    }
    worker_gateways: dict[str, Callable[[], ModelGateway]] = {
        "openai": get_openai_gateway,
    }
    realtime_gateways: dict[str, Callable[[], RealtimeModelGateway]] = {
        "gemini": get_gemini_realtime_gateway,
    }
    text_gateway = text_gateways[settings.text_agent_provider]()
    worker_gateway = worker_gateways[settings.worker_provider]()
    realtime_gateway = realtime_gateways[settings.realtime_agent_provider]()

    text_definition = AgentDefinition(
        model=settings.text_agent_model,
        instructions=settings.agent_instructions,
        tool_names=interactive_tools.names,
    )
    realtime_definition = AgentDefinition(
        model=settings.realtime_agent_model,
        instructions=f"{settings.agent_instructions}\n\n{settings.realtime_agent_instructions}",
        tool_names=interactive_tools.names,
    )
    worker_definition = AgentDefinition(
        model=settings.worker_agent_model,
        instructions=settings.worker_agent_instructions,
        tool_names=worker_tools.names,
    )
    text_agent_service = AgentService(
        model_gateway=text_gateway,
        tools=interactive_tools,
        conversations=conversations,
        max_tool_rounds=settings.max_tool_rounds,
    )
    worker_agent_service = AgentService(
        model_gateway=worker_gateway,
        tools=worker_tools,
        conversations=conversations,
        max_tool_rounds=settings.max_tool_rounds,
    )
    realtime_agent_service = RealtimeAgentService(
        model_gateway=realtime_gateway,
        tools=interactive_tools,
        conversations=conversations,
        interactions=interactions,
        notifier=interaction_notifier,
        max_audio_chunk_bytes=settings.realtime_audio_max_chunk_bytes,
        max_tool_rounds=settings.max_tool_rounds,
        max_session_seconds=settings.realtime_session_max_seconds,
        outbound_max_messages=settings.realtime_outbound_max_messages,
        outbound_max_audio_bytes=settings.realtime_outbound_max_audio_bytes,
        outbound_enqueue_timeout_seconds=settings.realtime_outbound_enqueue_timeout_seconds,
        proactive_turn_timeout_seconds=settings.realtime_proactive_turn_timeout_seconds,
        command_reconciliation_seconds=settings.realtime_command_reconciliation_seconds,
    )
    return ModelRuntime(
        text_agent=TurnInteractionAgent(text_agent_service, text_definition),
        text_agent_service=text_agent_service,
        text_definition=text_definition,
        worker_agent_service=worker_agent_service,
        worker_definition=worker_definition,
        realtime_agent_service=realtime_agent_service,
        realtime_definition=realtime_definition,
        text_agent_provider=settings.text_agent_provider,
        realtime_agent_provider=settings.realtime_agent_provider,
        worker_provider=settings.worker_provider,
        _closers=tuple(closers),
    )


def _validate_selection(settings: Settings) -> None:
    """Reject roles lacking a concrete provider adapter at composition time."""
    if settings.text_agent_provider != "openai":
        raise ValueError(f"Unsupported text agent provider: {settings.text_agent_provider}")
    if settings.realtime_agent_provider != "gemini":
        raise ValueError(f"Unsupported realtime agent provider: {settings.realtime_agent_provider}")
    if settings.worker_provider != "openai":
        raise ValueError(f"Unsupported worker provider: {settings.worker_provider}")
