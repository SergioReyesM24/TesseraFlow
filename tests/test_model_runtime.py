from typing import Any

import pytest

import infrastructure.model_runtime as runtime_module
from application.interactions import TurnInteractionAgent
from application.tools import ToolRegistry
from config import Settings
from domain.conversations import Conversation, ConversationItem, ConversationKey
from infrastructure.model_runtime import build_model_runtime


class StubConversations:
    """Provide the repository shape required while composing model services."""

    async def create(self, key: ConversationKey) -> Conversation:
        """Return an empty aggregate without external persistence."""
        return Conversation(key=key)

    async def load(self, key: ConversationKey) -> Conversation | None:
        """Return an empty owned aggregate."""
        return Conversation(key=key)

    async def save_turn(
        self,
        conversation: Conversation,
        turn: tuple[ConversationItem, ...],
    ) -> Conversation:
        """Append a neutral turn in memory."""
        return Conversation(
            key=conversation.key,
            messages=conversation.messages + turn,
            version=conversation.version + 1,
        )

    async def delete(self, key: ConversationKey) -> bool:
        """Provide the unused deletion operation."""
        del key
        return True


class FakeOpenAIClient:
    """Capture construction and lifecycle without contacting a provider."""

    instances: list["FakeOpenAIClient"] = []

    def __init__(self, **kwargs: object) -> None:
        """Record client options and expose a close flag."""
        self.kwargs = kwargs
        self.closed = False
        self.instances.append(self)

    async def close(self) -> None:
        """Record process-level cleanup."""
        self.closed = True


class FakeGeminiAsyncClient:
    """Expose the asynchronous close operation used by the runtime."""

    def __init__(self) -> None:
        """Initialize a close flag."""
        self.closed = False

    async def aclose(self) -> None:
        """Record process-level cleanup."""
        self.closed = True


class FakeGeminiClient:
    """Capture Gemini construction without opening a Live connection."""

    instances: list["FakeGeminiClient"] = []

    def __init__(self, **kwargs: object) -> None:
        """Record options and provide the async client facade."""
        self.kwargs = kwargs
        self.aio = FakeGeminiAsyncClient()
        self.instances.append(self)


def empty_tools() -> ToolRegistry:
    """Return an empty provider-neutral tool catalog."""
    return ToolRegistry([])


async def test_text_runtime_shares_one_openai_client_between_roles(
    monkeypatch: Any,
) -> None:
    """Select a text flow entirely by settings and encapsulate its shared client."""
    FakeOpenAIClient.instances = []
    monkeypatch.setattr(runtime_module, "AsyncOpenAI", FakeOpenAIClient)
    settings = Settings(
        interactive_flow="text",
        interactive_provider="openai",
        interactive_model="interactive-text-model",
        worker_provider="openai",
        worker_agent_model="worker-model",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
    )

    runtime = build_model_runtime(
        settings,
        conversations=StubConversations(),
        interactive_tools=empty_tools(),
        worker_tools=empty_tools(),
    )

    assert runtime.interactive_provider == "openai"
    assert runtime.default_agent.model == "interactive-text-model"
    assert runtime.worker_definition.model == "worker-model"
    assert "spoken as native audio" not in runtime.default_agent.instructions
    assert runtime.realtime_agent_service is None
    assert isinstance(runtime.interactive_agent, TurnInteractionAgent)
    assert len(FakeOpenAIClient.instances) == 1
    client = FakeOpenAIClient.instances[0]
    assert client.kwargs["api_key"] == "test-key"
    assert client.kwargs["base_url"] == "https://example.test/v1"
    await runtime.close()
    assert client.closed is True


async def test_live_runtime_owns_separate_gemini_and_openai_clients(
    monkeypatch: Any,
) -> None:
    """Compose different providers for interaction and heavy work from configuration."""
    FakeOpenAIClient.instances = []
    FakeGeminiClient.instances = []
    monkeypatch.setattr(runtime_module, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(runtime_module.genai, "Client", FakeGeminiClient)
    settings = Settings(
        interactive_flow="live_audio",
        interactive_provider="gemini",
        interactive_model="gemini-live-model",
        worker_provider="openai",
        worker_agent_model="openai-worker-model",
        gemini_api_key="gemini-key",
        openai_api_key="openai-key",
    )

    runtime = build_model_runtime(
        settings,
        conversations=StubConversations(),
        interactive_tools=empty_tools(),
        worker_tools=empty_tools(),
    )

    assert runtime.default_agent.model == "gemini-live-model"
    assert runtime.worker_definition.model == "openai-worker-model"
    assert "spoken as native audio" in runtime.default_agent.instructions
    assert runtime.realtime_agent_service is None
    assert len(FakeGeminiClient.instances) == 1
    assert len(FakeOpenAIClient.instances) == 1
    gemini = FakeGeminiClient.instances[0]
    openai = FakeOpenAIClient.instances[0]
    await runtime.close()
    assert gemini.aio.closed is True
    assert openai.closed is True


async def test_speech_to_speech_runtime_shares_one_gemini_client_for_both_gateways(
    monkeypatch: Any,
) -> None:
    """Compose turn fallback and full-duplex adapters over one Gemini client."""
    FakeOpenAIClient.instances = []
    FakeGeminiClient.instances = []
    monkeypatch.setattr(runtime_module, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(runtime_module.genai, "Client", FakeGeminiClient)
    settings = Settings(
        interactive_flow="speech_to_speech",
        interactive_provider="gemini",
        worker_provider="openai",
        gemini_api_key="gemini-key",
        openai_api_key="openai-key",
    )

    runtime = build_model_runtime(
        settings,
        conversations=StubConversations(),
        interactive_tools=empty_tools(),
        worker_tools=empty_tools(),
    )

    assert runtime.realtime_agent_service is not None
    assert len(FakeGeminiClient.instances) == 1
    assert len(FakeOpenAIClient.instances) == 1
    await runtime.close()
    assert FakeGeminiClient.instances[0].aio.closed is True
    assert FakeOpenAIClient.instances[0].closed is True


def test_runtime_rejects_unregistered_flow_provider_combinations() -> None:
    """Fail at composition instead of branching on providers inside the core."""
    settings = Settings(
        interactive_flow="text",
        interactive_provider="gemini",
        worker_provider="openai",
    )

    with pytest.raises(ValueError, match="Unsupported interactive"):
        build_model_runtime(
            settings,
            conversations=StubConversations(),
            interactive_tools=empty_tools(),
            worker_tools=empty_tools(),
        )
