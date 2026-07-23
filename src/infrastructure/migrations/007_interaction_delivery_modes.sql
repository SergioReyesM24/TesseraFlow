ALTER TABLE a2a_jobs
    ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'turn_based';

ALTER TABLE a2a_jobs
    DROP CONSTRAINT IF EXISTS a2a_jobs_delivery_mode_check;

ALTER TABLE a2a_jobs
    ADD CONSTRAINT a2a_jobs_delivery_mode_check
    CHECK (delivery_mode IN ('turn_based', 'realtime'));

ALTER TABLE interaction_commands
    ADD COLUMN IF NOT EXISTS delivery_mode TEXT NOT NULL DEFAULT 'turn_based';

ALTER TABLE interaction_commands
    DROP CONSTRAINT IF EXISTS interaction_commands_delivery_mode_check;

ALTER TABLE interaction_commands
    ADD CONSTRAINT interaction_commands_delivery_mode_check
    CHECK (delivery_mode IN ('turn_based', 'realtime'));

CREATE INDEX IF NOT EXISTS interaction_commands_realtime_claim_idx
    ON interaction_commands (conversation_id, status, sequence)
    WHERE delivery_mode = 'realtime' AND status IN ('queued', 'running');

CREATE OR REPLACE FUNCTION notify_interaction_command()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify(
        'tesseraflow_interaction_commands',
        json_build_object(
            'command_id', NEW.id,
            'conversation_id', NEW.conversation_id,
            'delivery_mode', NEW.delivery_mode
        )::text
    );
    RETURN NEW;
END;
$$;
