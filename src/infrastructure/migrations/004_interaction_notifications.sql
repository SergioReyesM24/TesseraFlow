CREATE OR REPLACE FUNCTION notify_interaction_command()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify('tesseraflow_interaction_commands', NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS interaction_command_notify_trigger ON interaction_commands;

CREATE TRIGGER interaction_command_notify_trigger
AFTER INSERT OR UPDATE OF status ON interaction_commands
FOR EACH ROW
WHEN (NEW.status = 'queued')
EXECUTE FUNCTION notify_interaction_command();

CREATE OR REPLACE FUNCTION notify_interaction_output()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify(
        'tesseraflow_interaction_outputs',
        json_build_object(
            'command_id', NEW.command_id,
            'conversation_id', NEW.conversation_id
        )::text
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS interaction_output_notify_trigger ON interaction_outbox;

CREATE TRIGGER interaction_output_notify_trigger
AFTER INSERT ON interaction_outbox
FOR EACH ROW
EXECUTE FUNCTION notify_interaction_output();
