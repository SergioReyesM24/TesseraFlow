from domain.turn_events import AgentAudioDelta, AgentAudioInterrupted, AgentVisualComponent
from domain.visuals import (
    ChartComponent,
    ChartPoint,
    ChartSeries,
    FinancialTransaction,
    TransactionListComponent,
    VisualPresentation,
)
from infrastructure.interaction_codec import decode_agent_event, encode_agent_event


def test_audio_events_round_trip_through_the_durable_json_codec() -> None:
    """Encode binary PCM explicitly without leaking bytes into PostgreSQL JSON."""
    delta = AgentAudioDelta(data=b"\x00\xff", mime_type="audio/pcm;rate=24000")

    event_type, payload = encode_agent_event(delta)

    assert event_type == "audio_delta"
    assert payload == {"audio": "AP8=", "mime_type": "audio/pcm;rate=24000"}
    assert decode_agent_event(event_type, payload) == delta
    assert decode_agent_event("audio_interrupted", {}) == AgentAudioInterrupted()


def test_visual_events_round_trip_through_the_durable_json_codec() -> None:
    """Persist only the neutral versioned schema and reconstruct its typed event."""
    event = AgentVisualComponent(
        presentation=VisualPresentation(
            component_id="history",
            fallback_text="El saldo aumentó durante el periodo.",
            component=ChartComponent(
                kind="chart",
                title="Historial",
                chart_type="line",
                series=(
                    ChartSeries(
                        name="Saldo",
                        points=(
                            ChartPoint(x="2026-07-01", y=100.0),
                            ChartPoint(x="2026-07-08", y=120.0),
                        ),
                    ),
                ),
                y_unit="EUR",
            ),
        )
    )

    event_type, payload = encode_agent_event(event)

    assert event_type == "visual_component"
    assert payload["schema"] == "tesseraflow.visual"
    assert payload["version"] == 1
    assert decode_agent_event(event_type, payload) == event


def test_transaction_visual_round_trips_through_the_durable_json_codec() -> None:
    """Preserve savings and movement semantics across durable event persistence."""
    event = AgentVisualComponent(
        presentation=VisualPresentation(
            component_id="transactions",
            fallback_text="Ahorro actual: 12.109,16 EUR.",
            component=TransactionListComponent(
                kind="transaction_list",
                title="Últimos movimientos",
                currency="EUR",
                base_savings=10000.0,
                current_savings=12109.16,
                total_income=2450.0,
                total_expenses=340.84,
                transactions=(
                    FinancialTransaction(
                        booked_at="2026-07-22T20:14:00+02:00",
                        merchant="La Tagliatella",
                        category="comida",
                        transaction_type="expense",
                        amount=38.6,
                        balance_after=12109.16,
                    ),
                ),
            ),
        )
    )

    event_type, payload = encode_agent_event(event)

    assert payload["component"]["kind"] == "transaction_list"
    assert decode_agent_event(event_type, payload) == event
