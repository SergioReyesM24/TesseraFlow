import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import ClassVar, Literal, cast

from application.tools import (
    AgentTool,
    ToolArguments,
    ToolExecutionContext,
    ToolExecutionOutput,
)
from domain.visuals import (
    FinancialTransaction,
    TransactionListComponent,
    VisualPresentation,
)

Sleeper = Callable[[float], Awaitable[None]]
BASE_SAVINGS = Decimal("10000.00")

RECENT_TRANSACTIONS = (
    (
        "txn-20260722-001",
        "22-07-2026 20:14:00+02:00",
        "La Tagliatella",
        "comida",
        "expense",
        "38.60",
    ),
    (
        "txn-20260722-002",
        "22-07-2026 08:32:00+02:00",
        "Repsol",
        "gasolina",
        "expense",
        "62.45",
    ),
    (
        "txn-20260721-001",
        "21-07-2026 18:47:00+02:00",
        "Mercadona",
        "supermercado",
        "expense",
        "54.28",
    ),
    ("txn-20260720-001", "20-07-2026 21:05:00+02:00", "Cinesa", "ocio", "expense", "19.80"),
    (
        "txn-20260719-001",
        "19-07-2026 10:16:00+02:00",
        "Renfe",
        "transporte",
        "expense",
        "27.35",
    ),
    (
        "txn-20260718-001",
        "18-07-2026 07:58:00+02:00",
        "Cafetería Central",
        "comida",
        "expense",
        "4.20",
    ),
    ("txn-20260717-001", "17-07-2026 09:00:00+02:00", "Nómina", "ingresos", "income", "2450.00"),
    (
        "txn-20260716-001",
        "16-07-2026 12:24:00+02:00",
        "Farmacia",
        "salud",
        "expense",
        "16.75",
    ),
    (
        "txn-20260715-001",
        "15-07-2026 06:30:00+02:00",
        "Iberdrola",
        "suministros",
        "expense",
        "71.42",
    ),
    ("txn-20260714-001", "14-07-2026 19:41:00+02:00", "Zara", "compras", "expense", "45.99"),
)


class RecentTransactionsArguments(ToolArguments):
    """Closed argument model for retrieving the latest account transactions."""


class RecentTransactionsTool(AgentTool[RecentTransactionsArguments]):
    """Return the latest categorized account transactions after a backend lookup."""

    name = "recent_transactions"
    description = (
        "Returns the ten latest account transactions in €, ordered from newest to oldest. "
        "The result starts from a base savings amount and includes current savings, income and "
        "expense totals. Each transaction identifies its type, merchant, category, positive "
        "amount and resulting balance. The lookup takes approximately five seconds."
    )
    arguments_model: ClassVar[type[RecentTransactionsArguments]] = RecentTransactionsArguments
    delay_seconds: ClassVar[float] = 5.0

    def __init__(self, sleeper: Sleeper = asyncio.sleep) -> None:
        """Accept an asynchronous sleeper so tests can avoid wall-clock delays."""
        self._sleeper = sleeper

    async def execute(
        self,
        arguments: RecentTransactionsArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Wait for the account lookup and return categorized transactions."""
        del arguments, context
        await self._sleeper(self.delay_seconds)
        running_balance = BASE_SAVINGS
        balances: dict[str, Decimal] = {}
        total_income = Decimal("0.00")
        total_expenses = Decimal("0.00")
        for transaction_id, _, _, _, transaction_type, raw_amount in reversed(
            RECENT_TRANSACTIONS
        ):
            amount = Decimal(raw_amount)
            if transaction_type == "income":
                total_income += amount
                running_balance += amount
            else:
                total_expenses += amount
                running_balance -= amount
            balances[transaction_id] = running_balance

        transactions = [
            {
                "transaction_id": transaction_id,
                "booked_at": booked_at,
                "merchant": merchant,
                "category": category,
                "transaction_type": transaction_type,
                "amount": float(amount),
                "balance_after": float(balances[transaction_id]),
            }
            for transaction_id, booked_at, merchant, category, transaction_type, amount in (
                RECENT_TRANSACTIONS
            )
        ]
        value = {
            "currency": "€",
            "base_savings": float(BASE_SAVINGS),
            "current_savings": float(running_balance),
            "total_income": float(total_income),
            "total_expenses": float(total_expenses),
            "net_change": float(total_income - total_expenses),
            "transactions": transactions,
        }
        presentation = VisualPresentation(
            component_id="recent-transactions",
            fallback_text=(
                f"El ahorro pasa de {float(BASE_SAVINGS):.2f} € a "
                f"{float(running_balance):.2f} € tras los últimos movimientos."
            ),
            component=TransactionListComponent(
                kind="transaction_list",
                title="Últimos movimientos",
                subtitle="Ingresos y gastos sobre el ahorro base",
                currency="€",
                base_savings=float(BASE_SAVINGS),
                current_savings=float(running_balance),
                total_income=float(total_income),
                total_expenses=float(total_expenses),
                transactions=tuple(
                    FinancialTransaction(
                        booked_at=booked_at,
                        merchant=merchant,
                        category=category,
                        transaction_type=cast(
                            Literal["income", "expense"],
                            transaction_type,
                        ),
                        amount=float(raw_amount),
                        balance_after=float(balances[transaction_id]),
                    )
                    for (
                        transaction_id,
                        booked_at,
                        merchant,
                        category,
                        transaction_type,
                        raw_amount,
                    ) in RECENT_TRANSACTIONS
                ),
            ),
        )
        return ToolExecutionOutput(value=value, visual_components=(presentation,))
