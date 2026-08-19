"""Validated presentation tool for the deliberately small visual v1 catalog."""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from application.tools import (
    AgentTool,
    ToolArguments,
    ToolExecutionContext,
    ToolExecutionOutput,
)
from domain.visuals import (
    MAX_CHART_POINTS,
    MAX_TRANSACTIONS,
    ChartComponent,
    ChartPoint,
    ChartSeries,
    FinancialTransaction,
    Metric,
    MetricGroupComponent,
    TransactionListComponent,
    VisualComponent,
    VisualPresentation,
)


class VisualArguments(BaseModel):
    """Closed base model shared by nested presentation arguments."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ChartPointArguments(VisualArguments):
    """One x label and finite numeric chart value."""

    x: str = Field(min_length=1, max_length=120)
    y: float


class ChartSeriesArguments(VisualArguments):
    """One bounded named chart series."""

    name: str = Field(min_length=1, max_length=80)
    points: list[ChartPointArguments] = Field(min_length=1, max_length=MAX_CHART_POINTS)


class ChartArguments(VisualArguments):
    """Semantic line or bar chart accepted from the model."""

    kind: Literal["chart"]
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(min_length=1, max_length=240)
    chart_type: Literal["line", "bar"]
    x_label: str | None = Field(min_length=1, max_length=80)
    y_label: str | None = Field(min_length=1, max_length=80)
    y_unit: str | None = Field(min_length=1, max_length=24)
    x_min: str | None = Field(
        min_length=1,
        max_length=120,
        description=(
            "Optional lower X-axis bound. Use an exact x label, date, or numeric string "
            "from the data when the chart should start later than the first point."
        ),
    )
    x_max: str | None = Field(
        min_length=1,
        max_length=120,
        description=(
            "Optional upper X-axis bound. Use an exact x label, date, or numeric string "
            "from the data when the chart should end before the last point."
        ),
    )
    y_min: float | None = Field(
        description="Optional lower Y-axis bound. Leave null to use the default scale."
    )
    y_max: float | None = Field(
        description="Optional upper Y-axis bound. Leave null to use the default scale."
    )
    series: list[ChartSeriesArguments] = Field(min_length=1, max_length=6)

    @model_validator(mode="before")
    @classmethod
    def default_axis_bounds(cls, data: Any) -> Any:
        """Accept older callers while keeping every tool-schema property explicitly required."""
        if not isinstance(data, dict):
            return data
        values = dict(data)
        for key in ("x_min", "x_max", "y_min", "y_max"):
            values.setdefault(key, None)
        return values

    @model_validator(mode="after")
    def validate_chart(self) -> "ChartArguments":
        """Reject oversized charts and ambiguous duplicate series."""
        if sum(len(item.points) for item in self.series) > MAX_CHART_POINTS:
            raise ValueError(f"chart cannot exceed {MAX_CHART_POINTS} total points")
        if len({item.name for item in self.series}) != len(self.series):
            raise ValueError("chart series names must be unique")
        if self.y_min is not None and self.y_max is not None and self.y_min >= self.y_max:
            raise ValueError("chart y minimum must be lower than chart y maximum")
        return self


class MetricArguments(VisualArguments):
    """One formatted metric without executable formatting instructions."""

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)
    unit: str | None = Field(min_length=1, max_length=24)
    detail: str | None = Field(min_length=1, max_length=160)


class MetricGroupArguments(VisualArguments):
    """Small related set of prominent values."""

    kind: Literal["metric_group", "metric-group"] = Field(
        description="Component discriminator; prefer metric_group with an underscore"
    )
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(min_length=1, max_length=240)
    metrics: list[MetricArguments] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_metrics(self) -> "MetricGroupArguments":
        """Require unambiguous labels inside one metric group."""
        if len({item.label for item in self.metrics}) != len(self.metrics):
            raise ValueError("metric labels must be unique")
        return self


class FinancialTransactionArguments(VisualArguments):
    """One income or expense relative to a resulting savings balance."""

    booked_at: str = Field(min_length=1, max_length=40)
    merchant: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=80)
    transaction_type: Literal["income", "expense"]
    amount: float = Field(gt=0)
    balance_after: float


class TransactionListArguments(VisualArguments):
    """Savings summary with its latest categorized movements."""

    kind: Literal["transaction_list", "transaction-list"] = Field(
        description="Component discriminator; prefer transaction_list with an underscore"
    )
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(min_length=1, max_length=240)
    currency: str = Field(min_length=1, max_length=8)
    base_savings: float
    current_savings: float
    total_income: float = Field(ge=0)
    total_expenses: float = Field(ge=0)
    transactions: list[FinancialTransactionArguments] = Field(
        min_length=1,
        max_length=MAX_TRANSACTIONS,
    )


ComponentArguments = ChartArguments | MetricGroupArguments | TransactionListArguments


class PresentVisualArguments(ToolArguments):
    """Version-one semantic component requested by the interactive agent."""

    placement: Literal["append", "replace"] = Field(
        description=(
            "Use append for every new visual. Use replace only when the user explicitly asks "
            "to modify an existing visual, and then reuse that visual's component_id"
        ),
    )
    component_id: str = Field(
        min_length=1,
        max_length=80,
        description="Stable kebab-case identifier unique within the current answer",
    )
    fallback_text: str = Field(
        min_length=1,
        max_length=500,
        description="Complete concise text conveying the component's meaning",
    )
    component: ComponentArguments


class PresentVisualTool(AgentTool[PresentVisualArguments]):
    """Publish one safe visual component as an application stream event."""

    name = "present_visual"
    description = (
        "Presents exact data already available in context as one safe visual component. "
        "Use a line chart for temporal trends with several points, a bar chart for category "
        "comparisons, a metric group for a few related headline values, or a transaction list "
        "for income and expenses tied to base and current savings. Do not use it for a single "
        "fact, uncertain data, or as a replacement for a concise textual answer. Never invent, "
        "interpolate, or transform source values."
    )
    arguments_model: ClassVar[type[PresentVisualArguments]] = PresentVisualArguments

    async def execute(
        self,
        arguments: PresentVisualArguments,
        context: ToolExecutionContext,
    ) -> object:
        """Convert closed Pydantic arguments to a neutral public presentation."""
        del context
        raw_component = arguments.component
        component: VisualComponent
        if isinstance(raw_component, ChartArguments):
            component = ChartComponent(
                kind="chart",
                title=raw_component.title,
                subtitle=raw_component.subtitle,
                chart_type=raw_component.chart_type,
                x_label=raw_component.x_label,
                y_label=raw_component.y_label,
                y_unit=raw_component.y_unit,
                x_min=raw_component.x_min,
                x_max=raw_component.x_max,
                y_min=raw_component.y_min,
                y_max=raw_component.y_max,
                series=tuple(
                    ChartSeries(
                        name=series.name,
                        points=tuple(ChartPoint(x=point.x, y=point.y) for point in series.points),
                    )
                    for series in raw_component.series
                ),
            )
        elif isinstance(raw_component, MetricGroupArguments):
            component = MetricGroupComponent(
                kind="metric_group",
                title=raw_component.title,
                subtitle=raw_component.subtitle,
                metrics=tuple(
                    Metric(
                        label=metric.label,
                        value=metric.value,
                        unit=metric.unit,
                        detail=metric.detail,
                    )
                    for metric in raw_component.metrics
                ),
            )
        else:
            component = TransactionListComponent(
                kind="transaction_list",
                title=raw_component.title,
                subtitle=raw_component.subtitle,
                currency=raw_component.currency,
                base_savings=raw_component.base_savings,
                current_savings=raw_component.current_savings,
                total_income=raw_component.total_income,
                total_expenses=raw_component.total_expenses,
                transactions=tuple(
                    FinancialTransaction(
                        booked_at=transaction.booked_at,
                        merchant=transaction.merchant,
                        category=transaction.category,
                        transaction_type=transaction.transaction_type,
                        amount=transaction.amount,
                        balance_after=transaction.balance_after,
                    )
                    for transaction in raw_component.transactions
                ),
            )
        presentation = VisualPresentation(
            component_id=arguments.component_id,
            fallback_text=arguments.fallback_text,
            component=component,
            placement=arguments.placement,
        )
        return ToolExecutionOutput(
            value={"presented": True, "component_id": arguments.component_id},
            visual_components=(presentation,),
        )
