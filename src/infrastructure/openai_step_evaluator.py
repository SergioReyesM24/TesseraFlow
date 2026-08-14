import asyncio
import json
from typing import Literal

import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from application.ports import AgentStepEvaluator
from domain.conversations import ConversationItem, ConversationMessage
from domain.evaluations import AgentStepEvaluation, AgentStepEvaluationRequest
from domain.tools import ToolCall, ToolSpec
from domain.types import JsonObject
from infrastructure.openai_usage import normalize_openai_usage

logger = structlog.get_logger(__name__)


class OpenAIEvaluationProtocolError(RuntimeError):
    """Raised when the evaluator does not return its required structured result."""


class _OpenAIToolCallEvaluation(BaseModel):
    """Strict provider response parsed before it crosses into the application."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "fail", "uncertain"]
    risk: Literal["low", "medium", "high"]
    reason_code: Literal[
        "none",
        "wrong_tool",
        "unnecessary_tool",
        "ungrounded_arguments",
        "duplicate_action",
        "unsafe_side_effect",
        "incomplete_request",
        "other",
    ]
    feedback: str = Field(max_length=1_000)


class OpenAIToolCallEvaluator(AgentStepEvaluator):
    """Evaluate neutral tool-call batches with OpenAI Structured Outputs."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str,
        instructions: str,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._model = model
        self._instructions = instructions
        self._timeout_seconds = timeout_seconds

    async def evaluate(self, request: AgentStepEvaluationRequest) -> AgentStepEvaluation:
        """Send bounded neutral evidence and normalize one structured judgment."""
        payload = json.dumps(
            self._request_payload(request),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        logger.info(
            "openai_tool_call_evaluation_started",
            model=self._model,
            role=request.role,
            context_item_count=len(request.context),
            tool_count=len(request.available_tools),
            tool_call_count=len(request.proposed_calls),
        )
        async with asyncio.timeout(self._timeout_seconds):
            response = await self._client.responses.parse(
                model=self._model,
                instructions=self._instructions,
                input=[{"role": "user", "content": payload}],
                text_format=_OpenAIToolCallEvaluation,
                store=False,
            )
        parsed = response.output_parsed
        if not isinstance(parsed, _OpenAIToolCallEvaluation):
            raise OpenAIEvaluationProtocolError(
                "OpenAI evaluator returned no structured tool-call evaluation"
            )
        logger.info(
            "openai_tool_call_evaluation_completed",
            model=self._model,
            request_id=getattr(response, "_request_id", None),
            verdict=parsed.verdict,
            risk=parsed.risk,
            reason_code=parsed.reason_code,
        )
        return AgentStepEvaluation(
            verdict=parsed.verdict,
            risk=parsed.risk,
            reason_code=parsed.reason_code,
            feedback=parsed.feedback,
            model=self._model,
            usage=normalize_openai_usage(getattr(response, "usage", None)),
        )

    @classmethod
    def _request_payload(cls, request: AgentStepEvaluationRequest) -> JsonObject:
        return {
            "protocol": "tesseraflow.tool_call_evaluation",
            "version": 1,
            "role": request.role,
            "conversation_context": [cls._context_item(item) for item in request.context],
            "available_tools": [cls._tool_spec(spec) for spec in request.available_tools],
            "proposed_calls": [cls._tool_call(call) for call in request.proposed_calls],
        }

    @staticmethod
    def _context_item(item: ConversationItem) -> JsonObject:
        if isinstance(item, ConversationMessage):
            return {
                "type": "message",
                "role": item.role,
                "source": item.source,
                "content": item.content,
            }
        if isinstance(item, ToolCall):
            return {
                "type": "tool_call",
                "call_id": item.call_id,
                "tool_name": item.tool_name,
                "arguments": item.arguments,
            }
        return {
            "type": "tool_result",
            "call_id": item.call_id,
            "output": item.output,
            "error": item.error,
        }

    @staticmethod
    def _tool_spec(spec: ToolSpec) -> JsonObject:
        return {
            "name": spec.name,
            "description": spec.description,
            "arguments_schema": spec.arguments_schema,
        }

    @staticmethod
    def _tool_call(call: ToolCall) -> JsonObject:
        return {
            "call_id": call.call_id,
            "tool_name": call.tool_name,
            "arguments": call.arguments,
        }
