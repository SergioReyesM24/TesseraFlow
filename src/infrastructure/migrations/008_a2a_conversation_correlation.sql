ALTER TABLE a2a_threads
    ADD COLUMN IF NOT EXISTS user_id TEXT;

UPDATE a2a_threads AS thread
SET user_id = parent.user_id
FROM conversations AS parent
WHERE parent.id = thread.parent_conversation_id
  AND thread.user_id IS NULL;

ALTER TABLE a2a_threads
    ALTER COLUMN user_id SET NOT NULL;

DROP INDEX IF EXISTS conversations_owner_updated_idx;
CREATE INDEX conversations_owner_updated_idx
    ON conversations (user_id, updated_at DESC);

DROP INDEX IF EXISTS a2a_threads_parent_idx;
CREATE INDEX a2a_threads_parent_idx
    ON a2a_threads (parent_conversation_id, user_id);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM a2a_threads AS thread
        JOIN conversations AS parent ON parent.id = thread.parent_conversation_id
        JOIN conversations AS worker ON worker.id = thread.worker_conversation_id
        WHERE thread.user_id <> parent.user_id
           OR parent.user_id <> worker.user_id
           OR thread.parent_conversation_id = thread.worker_conversation_id
           OR EXISTS (
               SELECT 1
               FROM a2a_threads AS parent_relation
               WHERE parent_relation.worker_conversation_id = thread.parent_conversation_id
           )
           OR EXISTS (
               SELECT 1
               FROM a2a_threads AS child_relation
               WHERE child_relation.parent_conversation_id = thread.worker_conversation_id
           )
    ) THEN
        RAISE EXCEPTION 'Existing A2A conversation correlations are ambiguous';
    END IF;
END;
$$;

ALTER TABLE a2a_threads
    DROP CONSTRAINT IF EXISTS a2a_threads_distinct_conversations_check;

ALTER TABLE a2a_threads
    ADD CONSTRAINT a2a_threads_distinct_conversations_check
    CHECK (parent_conversation_id <> worker_conversation_id);

CREATE OR REPLACE FUNCTION validate_a2a_conversation_correlation()
RETURNS TRIGGER AS $$
DECLARE
    parent_user_id TEXT;
    worker_user_id TEXT;
BEGIN
    PERFORM id
    FROM conversations
    WHERE id IN (NEW.parent_conversation_id, NEW.worker_conversation_id)
    ORDER BY id
    FOR UPDATE;

    SELECT user_id INTO parent_user_id
    FROM conversations
    WHERE id = NEW.parent_conversation_id;

    SELECT user_id INTO worker_user_id
    FROM conversations
    WHERE id = NEW.worker_conversation_id;

    IF parent_user_id IS NULL OR worker_user_id IS NULL THEN
        RAISE EXCEPTION 'A2A conversations must exist before their thread';
    END IF;
    IF parent_user_id <> worker_user_id THEN
        RAISE EXCEPTION 'A2A conversations must have the same owner';
    END IF;
    IF NEW.user_id <> parent_user_id THEN
        RAISE EXCEPTION 'A2A thread ownership must match its parent conversation';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM a2a_threads
        WHERE worker_conversation_id = NEW.parent_conversation_id
          AND id <> NEW.id
    ) OR EXISTS (
        SELECT 1
        FROM a2a_threads
        WHERE parent_conversation_id = NEW.worker_conversation_id
          AND id <> NEW.id
    ) THEN
        RAISE EXCEPTION 'A2A conversations cannot be both roots and workers';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS a2a_threads_validate_correlation ON a2a_threads;
CREATE TRIGGER a2a_threads_validate_correlation
BEFORE INSERT OR UPDATE OF parent_conversation_id, worker_conversation_id, user_id
ON a2a_threads
FOR EACH ROW EXECUTE FUNCTION validate_a2a_conversation_correlation();

COMMENT ON TABLE a2a_threads IS
    'Single source of truth relating isolated worker conversations to one root conversation';
