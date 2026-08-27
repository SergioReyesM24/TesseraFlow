from decimal import Decimal
from typing import Any

import pytest

import infrastructure.model_runtime as runtime_module
from application.interactions import TurnInteractionAgent
from application.tools import ToolRegistry
from config import Settings
from domain.conversations import Conversation, ConversationItem, ConversationKey
from domain.costs import ModelRates
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
        *,
        turn_id: str,
    ) -> Conversation:
        """Append a neutral turn in memory."""
        del turn_id
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


async def test_runtime_composes_independent_text_realtime_and_worker_roles(
    monkeypatch: Any,
) -> None:
    """Compose both endpoints while sharing clients only within each provider."""
    FakeOpenAIClient.instances = []
    FakeGeminiClient.instances = []
    monkeypatch.setattr(runtime_module, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(runtime_module.genai, "Client", FakeGeminiClient)
    settings = Settings(
        text_agent_provider="openai",
        text_agent_model="interactive-text-model",
        realtime_agent_provider="gemini",
        realtime_agent_model="realtime-model",
        worker_provider="openai",
        worker_agent_model="worker-model",
        interactive_tool_evaluation_mode="shadow",
        worker_tool_evaluation_mode="shadow",
        evaluation_model="evaluation-model",
        openai_api_key="test-key",
        openai_base_url="https://example.test/v1",
    )

    runtime = build_model_runtime(
        settings,
        conversations=StubConversations(),
        interactive_tools=empty_tools(),
        worker_tools=empty_tools(),
    )

    assert runtime.text_agent_provider == "openai"
    assert runtime.realtime_agent_provider == "gemini"
    assert runtime.text_definition.model == "interactive-text-model"
    assert runtime.realtime_definition.model == "realtime-model"
    assert runtime.worker_definition.model == "worker-model"
    assert "spoken as native audio" not in runtime.text_definition.instructions
    assert "spoken as native audio" in runtime.realtime_definition.instructions
    assert isinstance(runtime.text_agent, TurnInteractionAgent)
    interactive_gate = runtime.text_agent_service._tool_call_gate
    assert interactive_gate is not None
    assert runtime.realtime_agent_service._tool_call_gate is interactive_gate
    assert runtime.worker_agent_service._tool_call_gate is not interactive_gate
    assert runtime.evaluation_control.enabled is True
    assert len(FakeOpenAIClient.instances) == 1
    assert len(FakeGeminiClient.instances) == 1
    client = FakeOpenAIClient.instances[0]
    assert client.kwargs["api_key"] == "test-key"
    assert client.kwargs["base_url"] == "https://example.test/v1"
    await runtime.close()
    assert client.closed is True
    assert FakeGeminiClient.instances[0].aio.closed is True


async def test_runtime_registers_openai_realtime_on_the_shared_openai_client(
    monkeypatch: Any,
) -> None:
    """Select OpenAI STS without constructing Gemini or a duplicate OpenAI client."""
    FakeOpenAIClient.instances = []
    FakeGeminiClient.instances = []
    monkeypatch.setattr(runtime_module, "AsyncOpenAI", FakeOpenAIClient)
    monkeypatch.setattr(runtime_module.genai, "Client", FakeGeminiClient)
    settings = Settings(
        realtime_agent_provider="openai",
        realtime_agent_model="gpt-realtime-2.1",
        interactive_tool_evaluation_mode="off",
        worker_tool_evaluation_mode="off",
        openai_api_key="test-key",
    )

    runtime = build_model_runtime(
        settings,
        conversations=StubConversations(),
        interactive_tools=empty_tools(),
        worker_tools=empty_tools(),
    )

    assert runtime.realtime_agent_provider == "openai"
    assert runtime.realtime_definition.model == "gpt-realtime-2.1"
    assert runtime.realtime_agent_service.capabilities.input_audio_mime_type.endswith("rate=24000")
    assert runtime.evaluation_control.enabled is False
    assert len(FakeOpenAIClient.instances) == 1
    assert FakeGeminiClient.instances == []
    await runtime.close()


def test_runtime_rejects_unregistered_role_provider() -> None:
    """Fail at composition without provider checks leaking into the core."""
    settings = Settings(
        realtime_agent_provider="unsupported",
        worker_provider="openai",
    )

    with pytest.raises(ValueError, match="Unsupported realtime agent provider"):
        build_model_runtime(
            settings,
            conversations=StubConversations(),
            interactive_tools=empty_tools(),
            worker_tools=empty_tools(),
        )


def test_runtime_uses_current_role_prices_for_unknown_model_families() -> None:
    """Bind unknown names to the active text, realtime, and worker price cards."""
    text_rates = ModelRates(input=Decimal("1"), output=Decimal("2"))
    realtime_rates = ModelRates(input=Decimal("3"), output=Decimal("4"))

    fallbacks = runtime_module._current_model_fallbacks(
        {"current-text": text_rates, "current-realtime": realtime_rates},
        text_model="current-text",
        realtime_model="current-realtime",
        worker_model="missing-worker",
    )

    assert fallbacks["gpt"] is text_rates
    assert fallbacks["gemini"] is realtime_rates
    assert fallbacks["default"] is text_rates
