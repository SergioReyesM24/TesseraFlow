ALTER TABLE interaction_outbox
    DROP CONSTRAINT IF EXISTS interaction_outbox_event_type_check;

ALTER TABLE interaction_outbox
    ADD CONSTRAINT interaction_outbox_event_type_check
    CHECK (
        event_type IN (
            'audio_delta',
            'audio_interrupted',
            'text_delta',
            'tool_started',
            'tool_completed',
            'visual_component',
            'completed',
            'error'
        )
    );
