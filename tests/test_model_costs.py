from decimal import Decimal

from domain.costs import (
    ModelCostCalculator,
    ModelRates,
    ModelRateTier,
    ModelUsage,
    TurnMetrics,
)


def test_cost_calculator_prices_cached_text_and_audio_without_double_counting() -> None:
    """Apply independent token buckets from a model-keyed rate card."""
    calculator = ModelCostCalculator(
        {
            "any-model": ModelRates(
                input=Decimal("1"),
                cached_input=Decimal("0.25"),
                input_audio=Decimal("4"),
                output=Decimal("2"),
                output_audio=Decimal("8"),
            )
        }
    )

    call = calculator.metrics(
        "any-model",
        ModelUsage(
            input_tokens=1_000,
            cached_input_tokens=200,
            input_audio_tokens=300,
            output_tokens=400,
            output_audio_tokens=100,
            reasoning_tokens=50,
        ),
    )

    assert call.cost is not None
    assert call.cost.amount == Decimal("0.00315")
    assert call.cost.currency == "USD"


def test_turn_metrics_aggregate_calls_and_keep_unconfigured_cost_unknown() -> None:
    """Sum provider-neutral usage while refusing a misleading partial cost."""
    calculator = ModelCostCalculator(
        {"priced": ModelRates(input=Decimal("1"), output=Decimal("2"))}
    )
    metrics = TurnMetrics(
        calls=(
            calculator.metrics("priced", ModelUsage(input_tokens=10, output_tokens=2)),
            calculator.metrics("unpriced", ModelUsage(input_tokens=5, output_tokens=3)),
        )
    )

    assert metrics.usage == ModelUsage(input_tokens=15, output_tokens=5)
    assert metrics.cost is None


def test_cost_calculator_selects_the_large_context_tier_per_request() -> None:
    """Apply threshold pricing to the complete response, including its output."""
    calculator = ModelCostCalculator(
        {
            "tiered": ModelRates(
                input=Decimal("2"),
                cached_input=Decimal("0.2"),
                output=Decimal("10"),
                currency="€",
                tiers=(
                    ModelRateTier(
                        min_input_tokens=101,
                        input=Decimal("4"),
                        cached_input=Decimal("0.4"),
                        output=Decimal("15"),
                    ),
                ),
            )
        }
    )

    call = calculator.metrics(
        "tiered",
        ModelUsage(input_tokens=200, cached_input_tokens=50, output_tokens=20),
    )

    assert call.cost is not None
    assert call.cost.amount == Decimal("0.00092")
    assert call.cost.currency == "€"
