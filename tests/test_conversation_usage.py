from decimal import Decimal

import pytest

from domain.conversations import (
    ConversationUsageBreakdown,
    DailyTokenUsage,
    ModelUsageBreakdown,
)
from domain.costs import ModelCost, ModelUsage


def test_daily_usage_rejects_a_complete_flag_without_a_complete_cost() -> None:
    """Keep the public aggregate from representing unknown money as fully priced."""
    with pytest.raises(ValueError, match="pricing state is inconsistent"):
        DailyTokenUsage(
            day="13-08-2026",
            usage=ModelUsage(input_tokens=10),
            cost=None,
            turn_count=1,
            model_call_count=1,
            fully_priced=True,
        )


def test_conversation_usage_rejects_model_breakdowns_with_missing_calls() -> None:
    """Ensure detailed model totals reconcile with the conversation total."""
    model = ModelUsageBreakdown(
        model="model-a",
        usage=ModelUsage(input_tokens=10),
        cost=ModelCost(amount=Decimal("0.01"), currency="USD"),
        model_call_count=1,
        fully_priced=True,
    )

    with pytest.raises(ValueError, match="call count is inconsistent"):
        ConversationUsageBreakdown(
            conversation_id="root-1",
            title="Principal",
            role="interactive",
            thread_id=None,
            usage=ModelUsage(input_tokens=20),
            cost=ModelCost(amount=Decimal("0.02"), currency="USD"),
            turn_count=1,
            model_call_count=2,
            fully_priced=True,
            models=(model,),
        )
