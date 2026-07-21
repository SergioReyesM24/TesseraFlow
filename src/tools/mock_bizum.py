import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import ClassVar

from pydantic import Field

from application.tools import AgentTool, ToolArguments, ToolExecutionContext

MOCK_BIZUM_RECIPIENT = "Mamá"


class MockBizumArguments(ToolArguments):
    """Validated amount for a simulated payment to the fixed recipient."""

    amount: Decimal = Field(
        gt=0,
        max_digits=10,
        decimal_places=2,
        description="Positive EUR amount to send to the fixed mock recipient Mamá",
    )


class MockBizumTool(AgentTool[MockBizumArguments]):
    """Return a synthetic Bizum receipt without contacting a payment provider."""

    name = "send_mock_bizum_to_mom"
    description = (
        "Simulates sending a Bizum payment in EUR to the fixed recipient Mamá. This is a "
        "mock operation with no real financial effect. Requires the amount and returns a "
        "synthetic receipt."
    )
    arguments_model: ClassVar[type[MockBizumArguments]] = MockBizumArguments

    def __init__(self, uid_factory: Callable[[], uuid.UUID] = uuid.uuid4) -> None:
        """Accept an injectable receipt identifier factory for deterministic tests."""
        self._uid_factory = uid_factory

    async def execute(
        self,
        arguments: MockBizumArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Build a mock receipt for the validated amount and fixed recipient."""
        del context
        return {
            "mock": True,
            "status": "simulated",
            "operation_id": str(self._uid_factory()),
            "recipient": MOCK_BIZUM_RECIPIENT,
            "amount": str(arguments.amount),
            "currency": "EUR",
        }
