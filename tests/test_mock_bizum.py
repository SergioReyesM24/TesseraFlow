from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from application.tools import ToolExecutionContext
from tools.mock_bizum import MOCK_BIZUM_RECIPIENT, MockBizumArguments, MockBizumTool


async def test_returns_a_synthetic_receipt_for_mom() -> None:
    """Simulate the payment without calling an external financial provider."""
    operation_id = UUID("11111111-1111-4111-8111-111111111111")
    tool = MockBizumTool(uid_factory=lambda: operation_id)

    result = await tool.execute(
        MockBizumArguments(amount=Decimal("25.50")),
        ToolExecutionContext(conversation_id="worker-conversation", user_id="user-1"),
    )

    assert result == {
        "mock": True,
        "status": "simulated",
        "operation_id": str(operation_id),
        "recipient": MOCK_BIZUM_RECIPIENT,
        "amount": "25.50",
        "currency": "EUR",
    }


def test_rejects_non_positive_amounts_and_recipient_overrides() -> None:
    """Keep the amount valid and prevent the model from changing the fixed recipient."""
    with pytest.raises(ValidationError):
        MockBizumArguments(amount=Decimal("0"))
    with pytest.raises(ValidationError):
        MockBizumArguments.model_validate({"amount": "10", "recipient": "Otra persona"})


def test_declares_a_closed_schema_with_one_required_amount() -> None:
    """Expose only the amount because Mamá is fixed by the capability itself."""
    schema = MockBizumTool().spec().arguments_schema

    assert schema["required"] == ["amount"]
    assert set(schema["properties"]) == {"amount"}
    assert schema["additionalProperties"] is False
