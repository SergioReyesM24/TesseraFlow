import pytest

from application.tools import (
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    VisualComponentLimitError,
    extend_visual_components,
)
from domain.conversations import ConversationKey
from domain.tools import ToolCall
from domain.visuals import (
    ChartComponent,
    Metric,
    MetricGroupComponent,
    TransactionListComponent,
    VisualPresentation,
)
from tools.present_visual import PresentVisualTool


def execution_context() -> ToolExecutionContext:
    """Build one isolated owner context for presentation tests."""
    return ToolExecutionContext.from_conversation(
        ConversationKey(conversation_id="conversation-1", user_id="user-1")
    )


def test_present_visual_schema_is_closed_and_strict_compatible() -> None:
    """Require every object property while expressing optional values as nullable."""
    schema = PresentVisualTool().spec().arguments_schema
    object_schemas = [schema, *schema["$defs"].values()]

    for object_schema in object_schemas:
        assert object_schema["additionalProperties"] is False
        assert set(object_schema["required"]) == set(object_schema["properties"])


async def test_present_visual_produces_a_typed_chart_and_small_model_ack() -> None:
    """Keep public presentation data separate from the result sent back to the model."""
    executor = ToolExecutor()

    batch = await executor.execute(
        (
            ToolCall(
                call_id="visual-1",
                tool_name="present_visual",
                arguments={
                    "component_id": "weekly-balance",
                    "fallback_text": "El saldo termina la serie en 13.275,65 EUR.",
                    "component": {
                        "kind": "chart",
                        "title": "Saldo semanal",
                        "subtitle": "Últimas dos semanas",
                        "chart_type": "line",
                        "x_label": "Semana",
                        "y_label": "Saldo",
                        "y_unit": "EUR",
                        "series": [
                            {
                                "name": "Saldo al cierre",
                                "points": [
                                    {"x": "2026-07-12", "y": 13450.0},
                                    {"x": "2026-07-19", "y": 13275.65},
                                ],
                            }
                        ],
                    },
                },
            ),
        ),
        ToolRegistry([PresentVisualTool()]),
        execution_context(),
    )

    assert batch.results[0].output == {
        "presented": True,
        "component_id": "weekly-balance",
    }
    assert batch.records[0].output == batch.results[0].output
    assert len(batch.visual_components) == 1
    presentation = batch.visual_components[0]
    assert presentation.component_id == "weekly-balance"
    assert isinstance(presentation.component, ChartComponent)
    assert presentation.component.series[0].points[-1].y == 13275.65


async def test_present_visual_rejects_oversized_or_non_finite_charts() -> None:
    """Convert untrusted model payload violations to a structured tool error."""
    executor = ToolExecutor()

    batch = await executor.execute(
        (
            ToolCall(
                call_id="visual-invalid",
                tool_name="present_visual",
                arguments={
                    "component_id": "invalid",
                    "fallback_text": "Datos no disponibles como gráfica.",
                    "component": {
                        "kind": "chart",
                        "title": "Inválida",
                        "subtitle": None,
                        "chart_type": "line",
                        "x_label": None,
                        "y_label": None,
                        "y_unit": None,
                        "series": [{"name": "Serie", "points": [{"x": "A", "y": float("inf")}]}],
                    },
                },
            ),
        ),
        ToolRegistry([PresentVisualTool()]),
        execution_context(),
    )

    assert batch.results[0].error is not None
    assert batch.records[0].status == "error"
    assert batch.visual_components == ()


async def test_present_visual_supports_bounded_metric_groups() -> None:
    """Represent a few headline values without introducing generic cards or layout."""
    executor = ToolExecutor()

    batch = await executor.execute(
        (
            ToolCall(
                call_id="metrics-1",
                tool_name="present_visual",
                arguments={
                    "component_id": "balance-summary",
                    "fallback_text": "Saldo actual 13.275,65 EUR; variación semanal -1,30%.",
                    "component": {
                        "kind": "metric_group",
                        "title": "Resumen del saldo",
                        "subtitle": None,
                        "metrics": [
                            {
                                "label": "Saldo actual",
                                "value": "13.275,65",
                                "unit": "EUR",
                                "detail": None,
                            },
                            {
                                "label": "Variación",
                                "value": "-1,30",
                                "unit": "%",
                                "detail": None,
                            },
                        ],
                    },
                },
            ),
        ),
        ToolRegistry([PresentVisualTool()]),
        execution_context(),
    )

    assert isinstance(batch.visual_components[0].component, MetricGroupComponent)
    assert len(batch.visual_components[0].component.metrics) == 2


async def test_present_visual_supports_transactions_based_on_savings() -> None:
    """Represent income and expenses together with their base and resulting savings."""
    executor = ToolExecutor()

    batch = await executor.execute(
        (
            ToolCall(
                call_id="transactions-1",
                tool_name="present_visual",
                arguments={
                    "component_id": "recent-transactions",
                    "fallback_text": (
                        "El ahorro pasa de 10.000,00 EUR a 12.109,16 EUR tras los movimientos."
                    ),
                    "component": {
                        "kind": "transaction_list",
                        "title": "Últimos movimientos",
                        "subtitle": "Ingresos y gastos sobre el ahorro base",
                        "currency": "EUR",
                        "base_savings": 10000.0,
                        "current_savings": 12109.16,
                        "total_income": 2450.0,
                        "total_expenses": 340.84,
                        "transactions": [
                            {
                                "booked_at": "2026-07-22T20:14:00+02:00",
                                "merchant": "La Tagliatella",
                                "category": "comida",
                                "transaction_type": "expense",
                                "amount": 38.6,
                                "balance_after": 12109.16,
                            },
                            {
                                "booked_at": "2026-07-17T09:00:00+02:00",
                                "merchant": "Nómina",
                                "category": "ingresos",
                                "transaction_type": "income",
                                "amount": 2450.0,
                                "balance_after": 12315.84,
                            },
                        ],
                    },
                },
            ),
        ),
        ToolRegistry([PresentVisualTool()]),
        execution_context(),
    )

    component = batch.visual_components[0].component
    assert isinstance(component, TransactionListComponent)
    assert component.base_savings == 10000.0
    assert component.current_savings == 12109.16
    assert component.transactions[0].merchant == "La Tagliatella"
    assert component.transactions[1].transaction_type == "income"


def test_visual_output_is_limited_per_turn() -> None:
    """Bound the number of cards independently from the model's tool-call count."""
    presentation = VisualPresentation(
        component_id="summary",
        fallback_text="Saldo actual 100 EUR.",
        component=MetricGroupComponent(
            kind="metric_group",
            title="Resumen",
            metrics=(Metric(label="Saldo", value="100", unit="EUR"),),
        ),
    )
    current = [presentation, presentation, presentation]

    with pytest.raises(VisualComponentLimitError):
        extend_visual_components(current, (presentation,))

    assert len(current) == 3
