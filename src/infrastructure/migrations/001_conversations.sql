CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_message_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS conversations_owner_updated_idx
    ON conversations (user_id, tenant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_items (
    id BIGSERIAL PRIMARY KEY,
    conversation_id TEXT NOT NULL
        REFERENCES conversations(id) ON DELETE CASCADE,
    turn_id UUID NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    item_type TEXT NOT NULL
        CHECK (item_type IN ('message', 'tool_call', 'tool_result')),
    role TEXT CHECK (role IS NULL OR role IN ('user', 'assistant')),
    call_id TEXT,
    tool_name TEXT,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, sequence)
);

CREATE INDEX IF NOT EXISTS conversation_items_turn_idx
    ON conversation_items (conversation_id, turn_id, sequence);

CREATE INDEX IF NOT EXISTS conversation_items_call_idx
    ON conversation_items (conversation_id, call_id)
    WHERE call_id IS NOT NULL;
