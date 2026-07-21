from domain.events import AgentAudioDelta, AgentAudioInterrupted
from infrastructure.interaction_codec import decode_agent_event, encode_agent_event


def test_audio_events_round_trip_through_the_durable_json_codec() -> None:
    """Encode binary PCM explicitly without leaking bytes into PostgreSQL JSON."""
    delta = AgentAudioDelta(data=b"\x00\xff", mime_type="audio/pcm;rate=24000")

    event_type, payload = encode_agent_event(delta)

    assert event_type == "audio_delta"
    assert payload == {"audio": "AP8=", "mime_type": "audio/pcm;rate=24000"}
    assert decode_agent_event(event_type, payload) == delta
    assert decode_agent_event("audio_interrupted", {}) == AgentAudioInterrupted()
