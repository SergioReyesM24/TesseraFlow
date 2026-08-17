CREATE TABLE IF NOT EXISTS evaluation_traces (
    id UUID PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id UUID NOT NULL,
    job_id UUID,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    proposed_call_ids JSONB NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'uncertain')),
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    reason_code TEXT NOT NULL CHECK (
        reason_code IN (
            'none',
            'wrong_tool',
            'unnecessary_tool',
            'ungrounded_arguments',
            'duplicate_action',
            'unsafe_side_effect',
            'incomplete_request',
            'other'
        )
    ),
    feedback TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('shadow', 'enforce')),
    executed BOOLEAN NOT NULL,
    model TEXT NOT NULL,
    usage JSONB NOT NULL,
    latency_ms DOUBLE PRECISION NOT NULL CHECK (latency_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (jsonb_typeof(proposed_call_ids) = 'array'),
    CHECK (jsonb_array_length(proposed_call_ids) > 0),
    CHECK (jsonb_typeof(usage) = 'object')
);

CREATE INDEX IF NOT EXISTS evaluation_traces_conversation_created_idx
    ON evaluation_traces (conversation_id, created_at, attempt);

COMMENT ON TABLE evaluation_traces IS
    'Auditable evaluator decisions excluded from provider-facing conversation context';
