from decimal import Decimal

from application.tools import ToolExecutionContext, ToolExecutor, ToolRegistry
from domain.conversations import ConversationKey, DailyTokenUsage
from domain.costs import ModelCost, ModelUsage
from domain.tools import ToolCall
from domain.visuals import ChartComponent, MetricGroupComponent
from tools.application_usage import ApplicationUsageTool


class StubApplicationUsageService:
    """Capture owner-scoped usage queries and return deterministic daily records."""

    def __init__(self, days: tuple[DailyTokenUsage, ...]) -> None:
        self.days = days
        self.queries: list[tuple[str, int]] = []

    async def load_daily_token_usage(
        self,
        user_id: str,
        *,
        days: int,
    ) -> tuple[DailyTokenUsage, ...]:
        self.queries.append((user_id, days))
        return self.days


def execution_context() -> ToolExecutionContext:
    return ToolExecutionContext.from_conversation(
        ConversationKey(conversation_id="conversation-1", user_id="user-1")
    )


def usage_day(
    day: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost: str | None,
    fully_priced: bool,
) -> DailyTokenUsage:
    return DailyTokenUsage(
        day=day,
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
        ),
        cost=(
            ModelCost(amount=Decimal(cost), currency="USD") if cost is not None else None
        ),
        turn_count=1,
        model_call_count=2,
        fully_priced=fully_priced,
    )


def test_application_usage_schema_is_closed_bounded_and_strict_compatible() -> None:
    schema = ApplicationUsageTool(StubApplicationUsageService(())).spec().arguments_schema

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["days"]
    assert schema["properties"]["days"]["minimum"] == 1
    assert schema["properties"]["days"]["maximum"] == 60


async def test_application_usage_returns_exact_totals_and_attaches_three_visuals() -> None:
    service = StubApplicationUsageService(
        (
            usage_day(
                "14-08-2026",
                input_tokens=1_000,
                output_tokens=200,
                cached_tokens=400,
                cost="0.0012",
                fully_priced=True,
            ),
            usage_day(
                "15-08-2026",
                input_tokens=2_000,
                output_tokens=500,
                cached_tokens=1_000,
                cost="0.0025",
                fully_priced=True,
            ),
        )
    )
    batch = await ToolExecutor().execute(
        (
            ToolCall(
                call_id="usage-1",
                tool_name="get_application_usage",
                arguments={"days": 30},
            ),
        ),
        ToolRegistry([ApplicationUsageTool(service)]),
        execution_context(),
    )

    assert service.queries == [("user-1", 30)]
    assert batch.results[0].error is None
    assert batch.results[0].output["period"] == {"days": 30, "timezone": "UTC"}
    assert batch.results[0].output["usage"] == {
        "input_tokens": 3_000,
        "output_tokens": 700,
        "total_tokens": 3_700,
        "cached_input_tokens": 1_400,
        "uncached_input_tokens": 1_600,
        "cached_input_audio_tokens": 0,
        "reasoning_tokens": 0,
        "input_audio_tokens": 0,
        "output_audio_tokens": 0,
    }
    assert batch.results[0].output["cost"] == {"amount": 0.0037, "currency": "USD"}
    assert batch.results[0].output["turn_count"] == 2
    assert batch.results[0].output["model_call_count"] == 4
    assert batch.results[0].output["fully_priced"] is True
    assert len(batch.results[0].output["daily"]) == 2

    assert [item.component_id for item in batch.visual_components] == [
        "application-usage-summary",
        "application-token-usage",
        "application-cost-usage",
    ]
    assert isinstance(batch.visual_components[0].component, MetricGroupComponent)
    assert isinstance(batch.visual_components[1].component, ChartComponent)
    assert isinstance(batch.visual_components[2].component, ChartComponent)
    token_chart = batch.visual_components[1].component
    assert [series.name for series in token_chart.series] == ["Entrada", "Salida", "Caché"]
    assert token_chart.series[0].points[-1].y == 2_000


async def test_application_usage_does_not_visualize_an_incomplete_cost_total() -> None:
    service = StubApplicationUsageService(
        (
            usage_day(
                "15-08-2026",
                input_tokens=500,
                output_tokens=100,
                cached_tokens=0,
                cost=None,
                fully_priced=False,
            ),
        )
    )
    batch = await ToolExecutor().execute(
        (
            ToolCall(
                call_id="usage-partial",
                tool_name="get_application_usage",
                arguments={"days": 7},
            ),
        ),
        ToolRegistry([ApplicationUsageTool(service)]),
        execution_context(),
    )

    assert batch.results[0].output["cost"] is None
    assert batch.results[0].output["fully_priced"] is False
    assert [item.component_id for item in batch.visual_components] == [
        "application-usage-summary",
        "application-token-usage",
    ]
    summary = batch.visual_components[0].component
    assert isinstance(summary, MetricGroupComponent)
    cost_metric = next(metric for metric in summary.metrics if metric.label == "Coste calculado")
    assert cost_metric.value == "Parcial"
    assert cost_metric.unit is None
