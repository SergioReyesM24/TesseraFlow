CREATE TABLE IF NOT EXISTS a2a_threads (
    id TEXT PRIMARY KEY,
    parent_conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    worker_conversation_id TEXT NOT NULL UNIQUE
        REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    tenant_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS a2a_threads_parent_idx
    ON a2a_threads (parent_conversation_id, user_id, tenant_id);

CREATE TABLE IF NOT EXISTS a2a_jobs (
    sequence BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL
        REFERENCES a2a_threads(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    answer TEXT,
    response_id TEXT,
    error_code TEXT,
    worker_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CHECK (
        (status = 'completed' AND answer IS NOT NULL AND response_id IS NOT NULL)
        OR status <> 'completed'
    )
);

CREATE INDEX IF NOT EXISTS a2a_jobs_claim_idx
    ON a2a_jobs (status, sequence)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS a2a_jobs_thread_idx
    ON a2a_jobs (thread_id, sequence);

CREATE OR REPLACE FUNCTION delete_a2a_worker_conversation()
RETURNS TRIGGER AS $$
BEGIN
    DELETE FROM conversations WHERE id = OLD.worker_conversation_id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS a2a_threads_delete_worker_conversation ON a2a_threads;
CREATE TRIGGER a2a_threads_delete_worker_conversation
AFTER DELETE ON a2a_threads
FOR EACH ROW EXECUTE FUNCTION delete_a2a_worker_conversation();
