from decimal import Decimal

from domain.conversations import ConversationMessage
from domain.costs import (
    ModelCallMetrics,
    ModelCost,
    ModelUsage,
    TurnMetrics,
)
from infrastructure.conversation_codec import (
    decode_conversation_item,
    encode_conversation_item,
)


def test_assistant_metrics_round_trip_through_canonical_history_codec() -> None:
    """Persist exact per-call cost and usage without a provider-shaped payload."""
    message = ConversationMessage(
        role="assistant",
        content="Hecho",
        metrics=TurnMetrics(
            calls=(
                ModelCallMetrics(
                    model="model-a",
                    usage=ModelUsage(input_tokens=120, output_tokens=30),
                    cost=ModelCost(amount=Decimal("0.00009"), currency="USD"),
                ),
            )
        ),
    )

    encoded = encode_conversation_item(message)

    assert encoded["metrics"]["calls"][0]["cost"]["amount"] == "0.00009"  # type: ignore[index]
    assert decode_conversation_item(encoded) == message
