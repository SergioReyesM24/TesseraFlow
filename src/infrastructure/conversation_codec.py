from typing import Any, Literal, cast

from domain.conversations import ConversationItem, ConversationMessage
from domain.tools import ToolCall, ToolResult


def encode_conversation_item(item: ConversationItem) -> dict[str, object]:
    """Serialize one neutral history item with a stable type discriminator."""
    if isinstance(item, ConversationMessage):
        return {
            "type": "message",
            "role": item.role,
            "content": item.content,
            "source": item.source,
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
        return ConversationMessage(
            role=cast(Literal["user", "assistant"], role),
            content=content,
            source=cast(
                Literal["text_user", "speech_user", "worker_agent", "assistant"],
                source,
            ),
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
