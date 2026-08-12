UPDATE conversation_items
SET payload = REGEXP_REPLACE(
    REPLACE(payload::text, 'EUR', '€'),
    '([0-9]{4})-([0-9]{2})-([0-9]{2})',
    E'\\3-\\2-\\1',
    'g'
)::jsonb
WHERE payload::text ~ 'EUR|[0-9]{4}-[0-9]{2}-[0-9]{2}';

UPDATE interaction_outbox
SET payload = REGEXP_REPLACE(
    REPLACE(payload::text, 'EUR', '€'),
    '([0-9]{4})-([0-9]{2})-([0-9]{2})',
    E'\\3-\\2-\\1',
    'g'
)::jsonb
WHERE payload::text ~ 'EUR|[0-9]{4}-[0-9]{2}-[0-9]{2}';
