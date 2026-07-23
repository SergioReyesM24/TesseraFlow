"""Provider- and framework-neutral visual components exposed by agent turns.

The v1 catalog is intentionally small. It describes meaning and data, never
frontend components, styles, HTML, JavaScript, or executable interactions.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal, TypeAlias, cast

VISUAL_SCHEMA = "tesseraflow.visual"
VISUAL_SCHEMA_VERSION = 1
MAX_CHART_SERIES = 6
MAX_CHART_POINTS = 200
MAX_METRICS = 6


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One labelled finite value in a chart series."""

    x: str
    y: float

    def __post_init__(self) -> None:
        """Reject labels and numeric values unsafe for a bounded chart."""
        _require_text(self.x, "chart point x", maximum=120)
        if isinstance(self.y, bool) or not isfinite(self.y):
            raise ValueError("chart point y must be a finite number")


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """Named sequence of points rendered together for comparison."""

    name: str
    points: tuple[ChartPoint, ...]

    def __post_init__(self) -> None:
        """Require a useful series with a bounded display label."""
        _require_text(self.name, "chart series name", maximum=80)
        if not self.points:
            raise ValueError("chart series must contain at least one point")


@dataclass(frozen=True, slots=True)
class ChartComponent:
    """Semantic line or bar chart with explicit axes and series."""

    kind: Literal["chart"]
    title: str
    chart_type: Literal["line", "bar"]
    series: tuple[ChartSeries, ...]
    subtitle: str | None = None
    x_label: str | None = None
    y_label: str | None = None
    y_unit: str | None = None

    def __post_init__(self) -> None:
        """Enforce the v1 chart catalog and payload bounds."""
        if self.kind != "chart":
            raise ValueError("chart component kind must be chart")
        _require_text(self.title, "chart title", maximum=120)
        _optional_text(self.subtitle, "chart subtitle", maximum=240)
        _optional_text(self.x_label, "chart x label", maximum=80)
        _optional_text(self.y_label, "chart y label", maximum=80)
        _optional_text(self.y_unit, "chart y unit", maximum=24)
        if self.chart_type not in ("line", "bar"):
            raise ValueError("chart type must be line or bar")
        if not 1 <= len(self.series) <= MAX_CHART_SERIES:
            raise ValueError(f"chart must contain 1 to {MAX_CHART_SERIES} series")
        if len({item.name for item in self.series}) != len(self.series):
            raise ValueError("chart series names must be unique")
        if sum(len(item.points) for item in self.series) > MAX_CHART_POINTS:
            raise ValueError(f"chart cannot exceed {MAX_CHART_POINTS} total points")


@dataclass(frozen=True, slots=True)
class Metric:
    """One already-formatted key value intended for prominent reading."""

    label: str
    value: str
    unit: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """Bound every visible field without interpreting its presentation."""
        _require_text(self.label, "metric label", maximum=80)
        _require_text(self.value, "metric value", maximum=80)
        _optional_text(self.unit, "metric unit", maximum=24)
        _optional_text(self.detail, "metric detail", maximum=160)


@dataclass(frozen=True, slots=True)
class MetricGroupComponent:
    """Small group of related values presented as comparable metrics."""

    kind: Literal["metric_group"]
    title: str
    metrics: tuple[Metric, ...]
    subtitle: str | None = None

    def __post_init__(self) -> None:
        """Keep metric groups useful and visually bounded."""
        if self.kind != "metric_group":
            raise ValueError("metric group component kind must be metric_group")
        _require_text(self.title, "metric group title", maximum=120)
        _optional_text(self.subtitle, "metric group subtitle", maximum=240)
        if not 1 <= len(self.metrics) <= MAX_METRICS:
            raise ValueError(f"metric group must contain 1 to {MAX_METRICS} metrics")
        if len({item.label for item in self.metrics}) != len(self.metrics):
            raise ValueError("metric labels must be unique")


VisualComponent: TypeAlias = ChartComponent | MetricGroupComponent


@dataclass(frozen=True, slots=True)
class VisualPresentation:
    """Versioned component plus the text needed for graceful degradation."""

    component_id: str
    fallback_text: str
    component: VisualComponent

    def __post_init__(self) -> None:
        """Require stable correlation and a complete non-visual alternative."""
        _require_text(self.component_id, "component id", maximum=80)
        _require_text(self.fallback_text, "visual fallback text", maximum=500)


def visual_presentation_payload(presentation: VisualPresentation) -> dict[str, object]:
    """Encode one validated component using the stable public v1 schema."""
    component = presentation.component
    if isinstance(component, ChartComponent):
        encoded_component: dict[str, object] = {
            "kind": component.kind,
            "title": component.title,
            "subtitle": component.subtitle,
            "chart_type": component.chart_type,
            "x_axis": {"label": component.x_label},
            "y_axis": {"label": component.y_label, "unit": component.y_unit},
            "series": [
                {
                    "name": series.name,
                    "points": [{"x": point.x, "y": point.y} for point in series.points],
                }
                for series in component.series
            ],
        }
    else:
        encoded_component = {
            "kind": component.kind,
            "title": component.title,
            "subtitle": component.subtitle,
            "metrics": [
                {
                    "label": metric.label,
                    "value": metric.value,
                    "unit": metric.unit,
                    "detail": metric.detail,
                }
                for metric in component.metrics
            ],
        }
    return {
        "schema": VISUAL_SCHEMA,
        "version": VISUAL_SCHEMA_VERSION,
        "component_id": presentation.component_id,
        "fallback_text": presentation.fallback_text,
        "component": encoded_component,
    }


def visual_presentation_from_payload(raw: object) -> VisualPresentation:
    """Validate an untrusted stored or transported v1 visual payload."""
    payload = _object(raw, "visual presentation")
    if payload.get("schema") != VISUAL_SCHEMA:
        raise ValueError("visual schema is invalid")
    if payload.get("version") != VISUAL_SCHEMA_VERSION:
        raise ValueError("visual schema version is unsupported")
    component = _object(payload.get("component"), "visual component")
    kind = component.get("kind")
    decoded: VisualComponent
    if kind == "chart":
        decoded = _chart_from_payload(component)
    elif kind == "metric_group":
        decoded = _metric_group_from_payload(component)
    else:
        raise ValueError("visual component kind is unsupported")
    return VisualPresentation(
        component_id=_text(payload, "component_id"),
        fallback_text=_text(payload, "fallback_text"),
        component=decoded,
    )


def _chart_from_payload(component: dict[str, Any]) -> ChartComponent:
    """Decode one chart after checking every nested collection shape."""
    raw_series = component.get("series")
    if not isinstance(raw_series, list):
        raise ValueError("chart series must be a list")
    series: list[ChartSeries] = []
    for raw_item in raw_series:
        item = _object(raw_item, "chart series")
        raw_points = item.get("points")
        if not isinstance(raw_points, list):
            raise ValueError("chart points must be a list")
        points: list[ChartPoint] = []
        for raw_point in raw_points:
            point = _object(raw_point, "chart point")
            y = point.get("y")
            if isinstance(y, bool) or not isinstance(y, int | float):
                raise ValueError("chart point y must be a number")
            points.append(ChartPoint(x=_text(point, "x"), y=float(y)))
        series.append(ChartSeries(name=_text(item, "name"), points=tuple(points)))
    x_axis = _object(component.get("x_axis"), "chart x axis")
    y_axis = _object(component.get("y_axis"), "chart y axis")
    chart_type = component.get("chart_type")
    if chart_type not in ("line", "bar"):
        raise ValueError("chart type must be line or bar")
    return ChartComponent(
        kind="chart",
        title=_text(component, "title"),
        subtitle=_optional_payload_text(component, "subtitle"),
        chart_type=cast(Literal["line", "bar"], chart_type),
        x_label=_optional_payload_text(x_axis, "label"),
        y_label=_optional_payload_text(y_axis, "label"),
        y_unit=_optional_payload_text(y_axis, "unit"),
        series=tuple(series),
    )


def _metric_group_from_payload(component: dict[str, Any]) -> MetricGroupComponent:
    """Decode one metric group with closed scalar fields."""
    raw_metrics = component.get("metrics")
    if not isinstance(raw_metrics, list):
        raise ValueError("metrics must be a list")
    metrics = tuple(
        Metric(
            label=_text(item, "label"),
            value=_text(item, "value"),
            unit=_optional_payload_text(item, "unit"),
            detail=_optional_payload_text(item, "detail"),
        )
        for item in (_object(raw_item, "metric") for raw_item in raw_metrics)
    )
    return MetricGroupComponent(
        kind="metric_group",
        title=_text(component, "title"),
        subtitle=_optional_payload_text(component, "subtitle"),
        metrics=metrics,
    )


def _object(raw: object, name: str) -> dict[str, Any]:
    """Narrow an unknown JSON value to an object."""
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, Any], raw)


def _text(payload: dict[str, Any], name: str) -> str:
    """Read one mandatory JSON string."""
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_payload_text(payload: dict[str, Any], name: str) -> str | None:
    """Read one nullable JSON string."""
    value = payload.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _require_text(value: str, name: str, *, maximum: int) -> None:
    """Validate one required bounded semantic label."""
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")


def _optional_text(value: str | None, name: str, *, maximum: int) -> None:
    """Validate one optional bounded semantic label."""
    if value is not None:
        _require_text(value, name, maximum=maximum)
