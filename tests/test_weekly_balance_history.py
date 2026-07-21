from application.tools import ToolExecutionContext
from tools.weekly_balance_history import (
    MOCK_WEEKLY_BALANCES,
    WeeklyBalanceHistoryArguments,
    WeeklyBalanceHistoryTool,
)


async def test_returns_deterministic_mock_balances_after_five_second_delay() -> None:
    """Expose the complete fixture after requesting the configured artificial delay."""
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        """Record the requested sleep without delaying the unit test."""
        delays.append(delay)

    tool = WeeklyBalanceHistoryTool(sleeper=record_sleep)

    result = await tool.execute(
        WeeklyBalanceHistoryArguments(),
        ToolExecutionContext(conversation_id="worker-conversation", user_id="user-1"),
    )

    assert delays == [5.0]
    assert result == {
        "data_source": "mock",
        "account_id": "mock-account-001",
        "currency": "EUR",
        "period": "weekly",
        "weekly_balances": [
            {
                "week_start": week_start,
                "week_end": week_end,
                "closing_balance": closing_balance,
            }
            for week_start, week_end, closing_balance in MOCK_WEEKLY_BALANCES
        ],
    }


def test_declares_a_closed_empty_argument_schema() -> None:
    """Allow the worker to invoke the fixture without inventing account parameters."""
    schema = WeeklyBalanceHistoryTool().spec().arguments_schema

    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False
