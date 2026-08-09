from application.tools import ToolExecutionContext, ToolExecutionOutput
from domain.visuals import TransactionListComponent
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
    assert isinstance(result, ToolExecutionOutput)
    value = result.value
    assert value["currency"] == "€"
    assert value["base_savings"] == float(BASE_SAVINGS)
    assert value["current_savings"] == 12109.16
    assert value["total_income"] == 2450.0
    assert value["total_expenses"] == 340.84
    assert value["net_change"] == 2109.16
    assert len(value["transactions"]) == len(RECENT_TRANSACTIONS)
    assert value["transactions"][0] == {
        "transaction_id": "txn-20260722-001",
        "booked_at": "22-07-2026 20:14:00+02:00",
        "merchant": "La Tagliatella",
        "category": "comida",
        "transaction_type": "expense",
        "amount": 38.6,
        "balance_after": 12109.16,
    }
    assert value["transactions"][-1]["balance_after"] == 9954.01
    assert {"comida", "gasolina"} <= {
        transaction["category"] for transaction in value["transactions"]
    }
    assert {transaction["transaction_type"] for transaction in value["transactions"]} == {
        "income",
        "expense",
    }
    assert len(result.visual_components) == 1
    visual = result.visual_components[0]
    assert visual.component_id == "recent-transactions"
    assert isinstance(visual.component, TransactionListComponent)
    assert visual.component.current_savings == 12109.16


def test_declares_a_closed_empty_schema_without_fixture_labels() -> None:
    """Expose a parameterless lookup without implementation-detail labels."""
    spec = RecentTransactionsTool().spec()

    assert spec.arguments_schema["properties"] == {}
    assert spec.arguments_schema["additionalProperties"] is False
    assert "mock" not in spec.name.lower()
    assert "mock" not in spec.description.lower()
