from application.tools import ToolExecutionContext
from tools.recent_transactions import (
    BASE_SAVINGS,
    RECENT_TRANSACTIONS,
    RecentTransactionsArguments,
    RecentTransactionsTool,
)


async def test_returns_categorized_transactions_after_five_second_delay() -> None:
    """Return the complete transaction set after requesting the configured delay."""
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        """Record the requested sleep without delaying the unit test."""
        delays.append(delay)

    tool = RecentTransactionsTool(sleeper=record_sleep)

    result = await tool.execute(
        RecentTransactionsArguments(),
        ToolExecutionContext(conversation_id="worker-conversation", user_id="user-1"),
    )

    assert delays == [5.0]
    assert result["currency"] == "EUR"
    assert result["base_savings"] == float(BASE_SAVINGS)
    assert result["current_savings"] == 12109.16
    assert result["total_income"] == 2450.0
    assert result["total_expenses"] == 340.84
    assert result["net_change"] == 2109.16
    assert len(result["transactions"]) == len(RECENT_TRANSACTIONS)
    assert result["transactions"][0] == {
        "transaction_id": "txn-20260722-001",
        "booked_at": "2026-07-22T20:14:00+02:00",
        "merchant": "La Tagliatella",
        "category": "comida",
        "transaction_type": "expense",
        "amount": 38.6,
        "balance_after": 12109.16,
    }
    assert result["transactions"][-1]["balance_after"] == 9954.01
    assert {"comida", "gasolina"} <= {
        transaction["category"] for transaction in result["transactions"]
    }
    assert {transaction["transaction_type"] for transaction in result["transactions"]} == {
        "income",
        "expense",
    }


def test_declares_a_closed_empty_schema_without_fixture_labels() -> None:
    """Expose a parameterless lookup without implementation-detail labels."""
    spec = RecentTransactionsTool().spec()

    assert spec.arguments_schema["properties"] == {}
    assert spec.arguments_schema["additionalProperties"] is False
    assert "mock" not in spec.name.lower()
    assert "mock" not in spec.description.lower()
