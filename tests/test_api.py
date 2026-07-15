from collections.abc import AsyncIterator
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.middleware import request_logging_middleware
from api.routes import router
from domain.agent import AgentDefinition, AgentResult
from domain.conversations import ConversationKey
from domain.events import (
    AgentStreamCompleted,
    AgentStreamEvent,
    AgentTextDelta,
)


class StubAgentService:
    async def run(
        self, message: str, definition: AgentDefinition, key: ConversationKey
    ) -> AgentResult:
        assert message == "Hola"
        assert definition.model == "test-model"
        assert key == ConversationKey(conversation_id="conv-1", user_id="user-1")
        return AgentResult(
            answer="Hola, ¿en qué puedo ayudarte?",
            response_id="resp_test",
            conversation_id=key.conversation_id,
        )

    async def stream(
        self,
        message: str,
        definition: AgentDefinition,
        key: ConversationKey,
    ) -> AsyncIterator[AgentStreamEvent]:
        assert message == "Hola"
        assert definition.model == "test-model"
        yield AgentTextDelta(text="Hola")
        yield AgentStreamCompleted(
            result=AgentResult(
                answer="Hola, ¿en qué puedo ayudarte?",
                response_id="resp_stream",
                conversation_id=key.conversation_id,
            )
        )

    async def delete_conversation(self, key: ConversationKey) -> bool:
        assert key == ConversationKey(conversation_id="conv-1", user_id="user-1")
        return True


async def test_health_and_agent_endpoint() -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(
        agent_service=StubAgentService(),
        default_agent=AgentDefinition(
            model="test-model",
            instructions="Test",
            tool_names=(),
        ),
    )
    app.middleware("http")(request_logging_middleware)
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        payload = {"message": "Hola", "conversation_id": "conv-1", "user_id": "user-1"}
        response = await client.post("/v1/agent/run", json=payload)
        stream_response = await client.post("/v1/agent/stream", json=payload)
        delete_response = await client.delete("/v1/conversations/conv-1?user_id=user-1")

    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.json() == {
        "answer": "Hola, ¿en qué puedo ayudarte?",
        "response_id": "resp_test",
        "conversation_id": "conv-1",
        "tool_calls": [],
    }
    assert stream_response.status_code == 200
    assert stream_response.headers["content-type"].startswith("text/event-stream")
    assert 'event: text_delta\ndata: {"text":"Hola"}\n\n' in stream_response.text
    assert "event: completed" in stream_response.text
    assert '"response_id":"resp_stream"' in stream_response.text
    assert delete_response.json() == {"deleted": True}
