"""Validated presentation tool for the deliberately small visual v1 catalog."""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from application.tools import (
    AgentTool,
    ToolArguments,
    ToolExecutionContext,
    ToolExecutionOutput,
)
from domain.visuals import (
    MAX_CHART_POINTS,
    ChartComponent,
    ChartPoint,
    ChartSeries,
    Metric,
    MetricGroupComponent,
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
    series: list[ChartSeriesArguments] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_chart(self) -> "ChartArguments":
        """Reject oversized charts and ambiguous duplicate series."""
        if sum(len(item.points) for item in self.series) > MAX_CHART_POINTS:
            raise ValueError(f"chart cannot exceed {MAX_CHART_POINTS} total points")
        if len({item.name for item in self.series}) != len(self.series):
            raise ValueError("chart series names must be unique")
        return self


class MetricArguments(VisualArguments):
    """One formatted metric without executable formatting instructions."""

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=80)
    unit: str | None = Field(min_length=1, max_length=24)
    detail: str | None = Field(min_length=1, max_length=160)


class MetricGroupArguments(VisualArguments):
    """Small related set of prominent values."""

    kind: Literal["metric_group"]
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(min_length=1, max_length=240)
    metrics: list[MetricArguments] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_metrics(self) -> "MetricGroupArguments":
        """Require unambiguous labels inside one metric group."""
        if len({item.label for item in self.metrics}) != len(self.metrics):
            raise ValueError("metric labels must be unique")
        return self


ComponentArguments = ChartArguments | MetricGroupArguments


class PresentVisualArguments(ToolArguments):
    """Version-one semantic component requested by the interactive agent."""

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
        "comparisons, or a metric group for a few related headline values. Do not use it for "
        "a single fact, uncertain data, or as a replacement for a concise textual answer. "
        "Never invent, interpolate, or transform source values."
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
                series=tuple(
                    ChartSeries(
                        name=series.name,
                        points=tuple(ChartPoint(x=point.x, y=point.y) for point in series.points),
                    )
                    for series in raw_component.series
                ),
            )
        else:
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
        presentation = VisualPresentation(
            component_id=arguments.component_id,
            fallback_text=arguments.fallback_text,
            component=component,
        )
        return ToolExecutionOutput(
            value={"presented": True, "component_id": arguments.component_id},
            visual_components=(presentation,),
        )
