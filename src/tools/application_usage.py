"""Owner-scoped application usage lookup with safe semantic visuals."""

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Protocol

from pydantic import Field

from application.tools import (
    AgentTool,
    ToolArguments,
    ToolExecutionContext,
    ToolExecutionOutput,
)
from domain.conversations import DailyTokenUsage
from domain.costs import ModelCost, ModelUsage
from domain.visuals import (
    ChartComponent,
    ChartPoint,
    ChartSeries,
    Metric,
    MetricGroupComponent,
    VisualPresentation,
)

MAX_VISUAL_DAYS = 14


class ApplicationUsageReader(Protocol):
    """Small application query surface consumed by the interactive tool."""

    async def load_daily_token_usage(
        self,
        user_id: str,
        *,
        days: int,
    ) -> tuple[DailyTokenUsage, ...]:
        """Return persisted owner-scoped usage for a recent UTC window."""
        ...


class ApplicationUsageArguments(ToolArguments):
    """Bound the accounting window returned to the interactive model."""

    days: int = Field(
        ge=1,
        le=60,
        description="Number of recent UTC calendar days to inspect; use 30 when unspecified",
    )


@dataclass(frozen=True, slots=True)
class _UsageSummary:
    """Internally reconciled aggregate used by both JSON and visual projections."""

    usage: ModelUsage
    cost: ModelCost | None
    turn_count: int
    model_call_count: int
    fully_priced: bool


class ApplicationUsageTool(AgentTool[ApplicationUsageArguments]):
    """Inspect persisted usage for every conversation owned by the current user."""

    name = "get_application_usage"
    description = (
        "Returns the current user's persisted TesseraFlow model usage across interactive and "
        "worker conversations for a recent UTC day window. It includes token categories, model "
        "call and turn counts, configured cost when every call is priced in one currency, and a "
        "daily breakdown. The tool automatically presents a headline metric group plus token and "
        "cost charts when available, so never call present_visual for the same data. The in-flight "
        "turn is not included until it has completed and been persisted. Use 30 days when the user "
        "does not specify a period."
    )
    arguments_model: ClassVar[type[ApplicationUsageArguments]] = ApplicationUsageArguments

    def __init__(self, histories: ApplicationUsageReader) -> None:
        """Bind the owner-aware application service, not a concrete database adapter."""
        self._histories = histories

    async def execute(
        self,
        arguments: ApplicationUsageArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Return normalized accounting data and directly attached visual projections."""
        days = await self._histories.load_daily_token_usage(
            context.user_id,
            days=arguments.days,
        )
        summary = _summarize(days)
        return ToolExecutionOutput(
            value={
                "period": {"days": arguments.days, "timezone": "UTC"},
                "usage": _usage_payload(summary.usage),
                "cost": _cost_payload(summary.cost),
                "turn_count": summary.turn_count,
                "model_call_count": summary.model_call_count,
                "fully_priced": summary.fully_priced,
                "daily": [
                    {
                        "date": day.day,
                        "usage": _usage_payload(day.usage),
                        "cost": _cost_payload(day.cost),
                        "turn_count": day.turn_count,
                        "model_call_count": day.model_call_count,
                        "fully_priced": day.fully_priced,
                    }
                    for day in days
                ],
            },
            visual_components=_presentations(days, summary),
        )


def _summarize(days: tuple[DailyTokenUsage, ...]) -> _UsageSummary:
    """Aggregate daily records without representing partial or mixed-currency cost as total."""
    usage = ModelUsage()
    turn_count = 0
    model_call_count = 0
    costs: list[ModelCost] = []
    fully_priced = True
    for day in days:
        usage += day.usage
        turn_count += day.turn_count
        model_call_count += day.model_call_count
        fully_priced = fully_priced and day.fully_priced
        if day.cost is not None:
            costs.append(day.cost)

    currencies = {cost.currency for cost in costs}
    fully_priced = fully_priced and (model_call_count == 0 or len(costs) > 0)
    fully_priced = fully_priced and len(currencies) <= 1
    cost = None
    if fully_priced and model_call_count > 0 and len(currencies) == 1:
        cost = ModelCost(
            amount=sum((item.amount for item in costs), start=Decimal("0")),
            currency=costs[0].currency,
        )
    return _UsageSummary(
        usage=usage,
        cost=cost,
        turn_count=turn_count,
        model_call_count=model_call_count,
        fully_priced=fully_priced,
    )


def _presentations(
    days: tuple[DailyTokenUsage, ...],
    summary: _UsageSummary,
) -> tuple[VisualPresentation, ...]:
    """Build at most three bounded semantic components from the exact queried values."""
    presentations = [_summary_presentation(summary)]
    visual_days = days[-MAX_VISUAL_DAYS:]
    if visual_days:
        presentations.append(_token_presentation(visual_days))
    if visual_days and summary.cost is not None:
        presentations.append(_cost_presentation(visual_days, summary.cost.currency))
    return tuple(presentations)


def _summary_presentation(summary: _UsageSummary) -> VisualPresentation:
    """Present the most useful application-wide counters as one compact group."""
    if summary.model_call_count == 0:
        cost_value = "0"
        cost_unit = None
        cost_detail = "Sin llamadas al modelo"
    elif summary.cost is None:
        cost_value = "Parcial"
        cost_unit = None
        cost_detail = "Faltan tarifas o hay varias divisas"
    else:
        cost_value = _decimal_text(summary.cost.amount)
        cost_unit = summary.cost.currency
        cost_detail = "Todas las llamadas tienen tarifa"

    return VisualPresentation(
        component_id="application-usage-summary",
        fallback_text=(
            f"Uso persistido: {_integer_text(summary.usage.total_tokens)} tokens en "
            f"{_integer_text(summary.model_call_count)} llamadas; coste {cost_value}"
            f"{f' {cost_unit}' if cost_unit else ''}."
        ),
        component=MetricGroupComponent(
            kind="metric_group",
            title="Uso de TesseraFlow",
            subtitle="Todas tus conversaciones y agentes internos · UTC",
            metrics=(
                Metric(label="Tokens totales", value=_integer_text(summary.usage.total_tokens)),
                Metric(label="Entrada", value=_integer_text(summary.usage.input_tokens)),
                Metric(label="Salida", value=_integer_text(summary.usage.output_tokens)),
                Metric(
                    label="En caché",
                    value=_integer_text(summary.usage.cached_input_tokens),
                    detail=_cache_detail(summary.usage),
                ),
                Metric(
                    label="Llamadas",
                    value=_integer_text(summary.model_call_count),
                    detail=f"{_integer_text(summary.turn_count)} turnos",
                ),
                Metric(
                    label="Coste calculado",
                    value=cost_value,
                    unit=cost_unit,
                    detail=cost_detail,
                ),
            ),
        ),
    )


def _token_presentation(days: tuple[DailyTokenUsage, ...]) -> VisualPresentation:
    """Present recent input, output, and cached-input token trends."""
    first = days[0].day
    last = days[-1].day
    return VisualPresentation(
        component_id="application-token-usage",
        fallback_text=(
            f"Tendencia diaria de entrada, salida y caché entre {first} y {last}; "
            f"{sum(day.usage.total_tokens for day in days)} tokens totales en el tramo visible."
        ),
        component=ChartComponent(
            kind="chart",
            title="Consumo diario de tokens",
            subtitle=f"Últimos {len(days)} días del periodo consultado",
            chart_type="line",
            x_label="Día (UTC)",
            y_label="Tokens",
            series=(
                ChartSeries(
                    name="Entrada",
                    points=tuple(
                        ChartPoint(x=day.day, y=float(day.usage.input_tokens)) for day in days
                    ),
                ),
                ChartSeries(
                    name="Salida",
                    points=tuple(
                        ChartPoint(x=day.day, y=float(day.usage.output_tokens)) for day in days
                    ),
                ),
                ChartSeries(
                    name="Caché",
                    points=tuple(
                        ChartPoint(x=day.day, y=float(day.usage.cached_input_tokens))
                        for day in days
                    ),
                ),
            ),
        ),
    )


def _cost_presentation(
    days: tuple[DailyTokenUsage, ...],
    currency: str,
) -> VisualPresentation:
    """Present daily configured cost only when the whole queried window reconciles."""
    points = tuple(
        ChartPoint(x=day.day, y=float(day.cost.amount) if day.cost is not None else 0.0)
        for day in days
    )
    visible_total = sum(
        (day.cost.amount for day in days if day.cost is not None),
        start=Decimal("0"),
    )
    return VisualPresentation(
        component_id="application-cost-usage",
        fallback_text=(
            f"Tendencia diaria del coste calculado en {currency} entre {days[0].day} y "
            f"{days[-1].day}; total visible {_decimal_text(visible_total)} {currency}."
        ),
        component=ChartComponent(
            kind="chart",
            title="Coste diario calculado",
            subtitle=f"Últimos {len(days)} días con tarifas configuradas",
            chart_type="bar",
            x_label="Día (UTC)",
            y_label="Coste",
            y_unit=currency,
            series=(ChartSeries(name="Coste", points=points),),
        ),
    )


def _usage_payload(usage: ModelUsage) -> dict[str, int]:
    """Encode the provider-neutral token contract for the model-facing result."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "cached_input_audio_tokens": usage.cached_input_audio_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "input_audio_tokens": usage.input_audio_tokens,
        "output_audio_tokens": usage.output_audio_tokens,
    }


def _cost_payload(cost: ModelCost | None) -> dict[str, float | str] | None:
    """Encode configured monetary cost without leaking Decimal to provider SDKs."""
    if cost is None:
        return None
    return {"amount": float(cost.amount), "currency": cost.currency}


def _integer_text(value: int) -> str:
    """Format a prominent integer using the application's Spanish grouping convention."""
    return f"{value:,}".replace(",", ".")


def _decimal_text(value: Decimal) -> str:
    """Keep small model costs readable without unnecessary trailing zeroes."""
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _cache_detail(usage: ModelUsage) -> str:
    """Explain cached input relative to all input without duplicating UI calculations."""
    percentage = (
        round(usage.cached_input_tokens / usage.input_tokens * 100)
        if usage.input_tokens
        else 0
    )
    return f"{percentage}% de la entrada"
