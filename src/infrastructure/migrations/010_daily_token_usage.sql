CREATE INDEX IF NOT EXISTS conversation_items_assistant_usage_idx
    ON conversation_items (conversation_id, created_at)
    WHERE item_type = 'message' AND role = 'assistant';
