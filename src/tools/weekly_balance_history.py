import asyncio
from collections.abc import Awaitable, Callable
from typing import ClassVar

from application.tools import (
    AgentTool,
    ToolArguments,
    ToolExecutionContext,
    ToolExecutionOutput,
)
from domain.visuals import ChartComponent, ChartPoint, ChartSeries, VisualPresentation

Sleeper = Callable[[float], Awaitable[None]]

MOCK_WEEKLY_BALANCES = (
    ("25-05-2026", "31-05-2026", "12450.75"),
    ("01-06-2026", "07-06-2026", "11980.20"),
    ("08-06-2026", "14-06-2026", "12840.55"),
    ("15-06-2026", "21-06-2026", "12610.10"),
    ("22-06-2026", "28-06-2026", "13125.90"),
    ("29-06-2026", "05-07-2026", "12980.40"),
    ("06-07-2026", "12-07-2026", "13450.00"),
    ("13-07-2026", "19-07-2026", "13275.65"),
)


class WeeklyBalanceHistoryArguments(ToolArguments):
    """Closed argument model for the fixed mock balance history."""


class WeeklyBalanceHistoryTool(AgentTool[WeeklyBalanceHistoryArguments]):
    """Simulate a slow account lookup and return deterministic weekly balances."""

    name = "weekly_balance_history"
    description = (
        "Returns eight weeks of mock account closing balances in €. The lookup takes "
        "approximately two seconds and is intended for end-to-end worker-agent tests."
    )
    arguments_model: ClassVar[type[WeeklyBalanceHistoryArguments]] = WeeklyBalanceHistoryArguments
    delay_seconds: ClassVar[float] = 2.0

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
        value = {
            "data_source": "mock",
            "account_id": "mock-account-001",
            "currency": "€",
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
        first_balance = float(MOCK_WEEKLY_BALANCES[0][2])
        last_balance = float(MOCK_WEEKLY_BALANCES[-1][2])
        presentation = VisualPresentation(
            component_id="weekly-balance-history",
            fallback_text=(
                f"El saldo semanal pasa de {first_balance:.2f} € a "
                f"{last_balance:.2f} € en ocho semanas."
            ),
            component=ChartComponent(
                kind="chart",
                title="Historial semanal de saldo",
                subtitle="Saldo al cierre de las últimas ocho semanas",
                chart_type="line",
                x_label="Fin de semana",
                y_label="Saldo",
                y_unit="€",
                series=(
                    ChartSeries(
                        name="Saldo al cierre",
                        points=tuple(
                            ChartPoint(x=week_end, y=float(closing_balance))
                            for _, week_end, closing_balance in MOCK_WEEKLY_BALANCES
                        ),
                    ),
                ),
            ),
        )
        return ToolExecutionOutput(value=value, visual_components=(presentation,))
