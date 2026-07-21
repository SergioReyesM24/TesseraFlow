CREATE TABLE IF NOT EXISTS interaction_commands (
    sequence BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL
        CHECK (kind IN ('user_message', 'worker_completed')),
    source TEXT NOT NULL
        CHECK (source IN ('text_user', 'speech_user', 'worker_agent')),
    message TEXT NOT NULL,
    causation_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    worker_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS interaction_commands_causation_idx
    ON interaction_commands (conversation_id, kind, causation_id)
    WHERE causation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS interaction_commands_claim_idx
    ON interaction_commands (status, sequence)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS interaction_commands_conversation_idx
    ON interaction_commands (conversation_id, sequence);

CREATE TABLE IF NOT EXISTS interaction_outbox (
    sequence BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    command_id TEXT NOT NULL
        REFERENCES interaction_commands(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    modality TEXT NOT NULL CHECK (modality IN ('text', 'audio')),
    event_type TEXT NOT NULL
        CHECK (event_type IN ('text_delta', 'tool_started', 'tool_completed', 'completed', 'error')),
    payload JSONB NOT NULL,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS interaction_outbox_delivery_idx
    ON interaction_outbox (conversation_id, sequence)
    WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS interaction_outbox_command_idx
    ON interaction_outbox (command_id, sequence);
