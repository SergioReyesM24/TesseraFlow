import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar

from application.tools import AgentTool, ToolArguments, ToolExecutionContext

Sleeper = Callable[[float], Awaitable[None]]

MOCK_WEEKLY_BALANCES = (
    ("2026-05-25", "2026-05-31", "12450.75"),
    ("2026-06-01", "2026-06-07", "11980.20"),
    ("2026-06-08", "2026-06-14", "12840.55"),
    ("2026-06-15", "2026-06-21", "12610.10"),
    ("2026-06-22", "2026-06-28", "13125.90"),
    ("2026-06-29", "2026-07-05", "12980.40"),
    ("2026-07-06", "2026-07-12", "13450.00"),
    ("2026-07-13", "2026-07-19", "13275.65"),
)


class WeeklyBalanceHistoryArguments(ToolArguments):
    """Closed argument model for the fixed mock balance history."""


class WeeklyBalanceHistoryTool(AgentTool[WeeklyBalanceHistoryArguments]):
    """Simulate a slow account lookup and return deterministic weekly balances."""

    name = "weekly_balance_history"
    description = (
        "Returns eight weeks of mock account closing balances in EUR. The lookup takes "
        "approximately five seconds and is intended for end-to-end worker-agent tests."
    )
    arguments_model: ClassVar[type[WeeklyBalanceHistoryArguments]] = WeeklyBalanceHistoryArguments
    delay_seconds: ClassVar[float] = 5.0

    def __init__(self, sleeper: Sleeper = asyncio.sleep) -> None:
        """Accept an asynchronous sleeper so tests can avoid wall-clock delays."""
        self._sleeper = sleeper

    async def execute(
        self,
        arguments: WeeklyBalanceHistoryArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Wait for the simulated backend and return a JSON-serializable fixture."""
        del arguments, context
        await self._sleeper(self.delay_seconds)
        return {
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
