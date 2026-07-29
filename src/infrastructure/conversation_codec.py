from decimal import Decimal
from typing import Any, Literal, cast

from domain.conversations import ConversationItem, ConversationMessage
from domain.costs import ModelCallMetrics, ModelCost, ModelUsage, TurnMetrics
from domain.tools import ToolCall, ToolResult


def encode_conversation_item(item: ConversationItem) -> dict[str, object]:
    """Serialize one neutral history item with a stable type discriminator."""
    if isinstance(item, ConversationMessage):
        payload: dict[str, object] = {
            "type": "message",
            "role": item.role,
            "content": item.content,
            "source": item.source,
        }
        if item.metrics is not None:
            payload["metrics"] = encode_turn_metrics(item.metrics)
        return payload
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


def decode_conversation_item(raw: object) -> ConversationItem:
    """Validate one decoded storage payload before rebuilding a domain item."""
    if not isinstance(raw, dict):
        raise ValueError("conversation item must be an object")
    item = cast(dict[str, Any], raw)
    item_type = item.get("type", "message")
    if item_type == "message":
        role = item.get("role")
        content = item.get("content")
        source = item.get("source", "assistant" if role == "assistant" else "text_user")
        if (
            role not in ("user", "assistant")
            or not isinstance(content, str)
            or source not in ("text_user", "speech_user", "worker_agent", "assistant")
        ):
            raise ValueError("message fields are invalid")
        metrics = decode_turn_metrics(item.get("metrics"))
        return ConversationMessage(
            role=cast(Literal["user", "assistant"], role),
            content=content,
            source=cast(
                Literal["text_user", "speech_user", "worker_agent", "assistant"],
                source,
            ),
            metrics=metrics,
        )
    if item_type == "tool_call":
        call_id = item.get("call_id")
        tool_name = item.get("tool_name")
        arguments = item.get("arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(arguments, dict)
        ):
            raise ValueError("tool call fields are invalid")
        return ToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments)
    if item_type == "tool_result":
        call_id = item.get("call_id")
        error = item.get("error")
        if not isinstance(call_id, str) or error is not None and not isinstance(error, str):
            raise ValueError("tool result fields are invalid")
        return ToolResult(call_id=call_id, output=item.get("output"), error=error)
    raise ValueError("conversation item type is invalid")


def encode_turn_metrics(metrics: TurnMetrics) -> dict[str, object]:
    """Encode exact neutral metrics without provider-shaped fields."""
    return {
        "calls": [
            {
                "model": call.model,
                "usage": {
                    "input_tokens": call.usage.input_tokens,
                    "output_tokens": call.usage.output_tokens,
                    "cached_input_tokens": call.usage.cached_input_tokens,
                    "cached_input_audio_tokens": call.usage.cached_input_audio_tokens,
                    "reasoning_tokens": call.usage.reasoning_tokens,
                    "input_audio_tokens": call.usage.input_audio_tokens,
                    "output_audio_tokens": call.usage.output_audio_tokens,
                },
                "cost": (
                    {
                        "amount": str(call.cost.amount),
                        "currency": call.cost.currency,
                    }
                    if call.cost is not None
                    else None
                ),
            }
            for call in metrics.calls
        ]
    }


def decode_turn_metrics(raw: object) -> TurnMetrics | None:
    """Decode optional metrics while remaining backwards compatible with old rows."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("calls"), list):
        raise ValueError("message metrics are invalid")
    calls: list[ModelCallMetrics] = []
    for raw_call in raw["calls"]:
        if not isinstance(raw_call, dict):
            raise ValueError("model call metrics are invalid")
        model = raw_call.get("model")
        raw_usage = raw_call.get("usage")
        raw_cost = raw_call.get("cost")
        if not isinstance(model, str) or not isinstance(raw_usage, dict):
            raise ValueError("model call metrics are invalid")
        try:
            usage = ModelUsage(
                input_tokens=int(raw_usage.get("input_tokens", 0)),
                output_tokens=int(raw_usage.get("output_tokens", 0)),
                cached_input_tokens=int(raw_usage.get("cached_input_tokens", 0)),
                cached_input_audio_tokens=int(
                    raw_usage.get("cached_input_audio_tokens", 0)
                ),
                reasoning_tokens=int(raw_usage.get("reasoning_tokens", 0)),
                input_audio_tokens=int(raw_usage.get("input_audio_tokens", 0)),
                output_audio_tokens=int(raw_usage.get("output_audio_tokens", 0)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("model usage metrics are invalid") from exc
        cost = None
        if raw_cost is not None:
            if not isinstance(raw_cost, dict):
                raise ValueError("model cost metrics are invalid")
            amount = raw_cost.get("amount")
            currency = raw_cost.get("currency")
            if not isinstance(amount, (str, int, float)) or not isinstance(currency, str):
                raise ValueError("model cost metrics are invalid")
            try:
                cost = ModelCost(amount=Decimal(str(amount)), currency=currency)
            except (ValueError, ArithmeticError) as exc:
                raise ValueError("model cost metrics are invalid") from exc
        calls.append(ModelCallMetrics(model=model, usage=usage, cost=cost))
    return TurnMetrics(calls=tuple(calls))
