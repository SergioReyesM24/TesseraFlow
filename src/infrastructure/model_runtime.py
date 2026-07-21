from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from google import genai
from openai import AsyncOpenAI

from application.agent import AgentService
from application.interactions import TurnInteractionAgent
from application.ports import ConversationRepository, InteractiveAgent, ModelGateway
from application.realtime import RealtimeAgentService
from application.tools import ToolRegistry
from config import Settings
from domain.agent import AgentDefinition
from infrastructure.gemini_live_gateway import GeminiLiveGateway
from infrastructure.gemini_realtime_gateway import GeminiRealtimeGateway
from infrastructure.openai_gateway import OpenAIResponsesGateway

AsyncCloser = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    """Provider-neutral services, definitions, and owned client lifecycle."""

    interactive_agent: InteractiveAgent
    agent_service: AgentService
    default_agent: AgentDefinition
    worker_agent_service: AgentService
    worker_definition: AgentDefinition
    realtime_agent_service: RealtimeAgentService | None
    interactive_provider: str
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
) -> ModelRuntime:
    """Select provider adapters by configuration and compose shared orchestration."""
    _validate_selection(settings)
    closers: list[AsyncCloser] = []
    openai_gateway: OpenAIResponsesGateway | None = None
    gemini_client: genai.Client | None = None
    gemini_gateway: GeminiLiveGateway | None = None
    gemini_realtime_gateway: GeminiRealtimeGateway | None = None

    def get_openai_gateway() -> OpenAIResponsesGateway:
        """Create one shared OpenAI client lazily for every selected role."""
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
        """Create one shared Gemini client lazily for turn and realtime gateways."""
        nonlocal gemini_client
        if gemini_client is None:
            gemini_client = genai.Client(
                api_key=settings.gemini_api_key or "missing-api-key",
                http_options=genai.types.HttpOptions(api_version=settings.gemini_live_api_version),
            )
            closers.append(gemini_client.aio.aclose)
        return gemini_client

    def get_gemini_gateway() -> GeminiLiveGateway:
        """Create the turn-based Gemini adapter over the shared provider client."""
        nonlocal gemini_gateway
        if gemini_gateway is None:
            gemini_gateway = GeminiLiveGateway(
                get_gemini_client(),
                voice_name=settings.gemini_live_voice_name,
                input_language_code=settings.gemini_live_language_code,
            )
        return gemini_gateway

    def get_gemini_realtime_gateway() -> GeminiRealtimeGateway:
        """Create the full-duplex Gemini adapter over the shared provider client."""
        nonlocal gemini_realtime_gateway
        if gemini_realtime_gateway is None:
            gemini_realtime_gateway = GeminiRealtimeGateway(
                get_gemini_client(),
                voice_name=settings.gemini_live_voice_name,
                input_language_code=settings.gemini_live_language_code,
            )
        return gemini_realtime_gateway

    gateways: dict[str, Callable[[], ModelGateway]] = {
        "openai": get_openai_gateway,
        "gemini": get_gemini_gateway,
    }
    interactive_gateway = gateways[settings.interactive_provider]()
    worker_gateway = gateways[settings.worker_provider]()
    default_agent = AgentDefinition(
        model=_interactive_model(settings),
        instructions=_interactive_instructions(settings),
        tool_names=interactive_tools.names,
    )
    worker_definition = AgentDefinition(
        model=settings.worker_agent_model or settings.openai_model,
        instructions=settings.worker_agent_instructions,
        tool_names=worker_tools.names,
    )
    agent_service = AgentService(
        model_gateway=interactive_gateway,
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
    realtime_agent_service = None
    if settings.interactive_flow == "speech_to_speech":
        realtime_agent_service = RealtimeAgentService(
            model_gateway=get_gemini_realtime_gateway(),
            tools=interactive_tools,
            conversations=conversations,
            max_audio_chunk_bytes=settings.realtime_audio_max_chunk_bytes,
            max_tool_rounds=settings.max_tool_rounds,
            max_session_seconds=settings.realtime_session_max_seconds,
        )
    return ModelRuntime(
        interactive_agent=TurnInteractionAgent(agent_service, default_agent),
        agent_service=agent_service,
        default_agent=default_agent,
        worker_agent_service=worker_agent_service,
        worker_definition=worker_definition,
        realtime_agent_service=realtime_agent_service,
        interactive_provider=settings.interactive_provider,
        worker_provider=settings.worker_provider,
        _closers=tuple(closers),
    )


def _validate_selection(settings: Settings) -> None:
    """Reject provider and flow combinations without a concrete adapter."""
    supported_interactive = {
        ("text", "openai"),
        ("live_audio", "gemini"),
        ("speech_to_speech", "gemini"),
    }
    selection = (settings.interactive_flow, settings.interactive_provider)
    if selection not in supported_interactive:
        raise ValueError(
            "Unsupported interactive flow/provider combination: "
            f"{settings.interactive_flow}/{settings.interactive_provider}"
        )
    if settings.worker_provider != "openai":
        raise ValueError(f"Unsupported worker provider: {settings.worker_provider}")


def _interactive_model(settings: Settings) -> str:
    """Resolve the explicit interactive model or its provider-compatible default."""
    if settings.interactive_model is not None:
        return settings.interactive_model
    if settings.interactive_provider == "gemini":
        return settings.gemini_live_model
    return settings.openai_model


def _interactive_instructions(settings: Settings) -> str:
    """Add voice-specific guidance only when the selected flow produces audio."""
    if settings.interactive_flow in ("live_audio", "speech_to_speech"):
        return f"{settings.agent_instructions}\n\n{settings.live_audio_instructions}"
    return settings.agent_instructions
