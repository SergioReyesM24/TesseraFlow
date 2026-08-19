-- Reconcile historical calls that were persisted before their model rate was configured.
-- The current price card is expressed per million tokens in EUR, matching the active .env.
WITH price_card(model, input_rate, cached_input_rate, input_audio_rate,
                output_rate, output_audio_rate, currency) AS (
    VALUES
        ('gpt-5-mini', 0.219684::numeric, 0.021968::numeric, NULL::numeric,
         1.757469::numeric, NULL::numeric, 'EUR'),
        ('gpt-5.4-mini', 0.66::numeric, 0.07::numeric, NULL::numeric,
         3.96::numeric, NULL::numeric, 'EUR'),
        ('gpt-5.6-luna', 0.88::numeric, 0.09::numeric, NULL::numeric,
         5.28::numeric, NULL::numeric, 'EUR'),
        ('gpt-5.4', 2.193945::numeric, 0.219394::numeric, NULL::numeric,
         13.163668::numeric, NULL::numeric, 'EUR'),
        ('gemini-3.1-flash-live-preview', 0.659051::numeric, NULL::numeric,
         2.636204::numeric, 3.954306::numeric, 10.544815::numeric, 'EUR')
), calls AS (
    SELECT item.id AS item_id,
           item.payload,
           call.value AS raw_call,
           call.ordinality,
           call.value->>'model' AS model,
           COALESCE((call.value #>> '{usage,input_tokens}')::numeric, 0) AS input_tokens,
           COALESCE((call.value #>> '{usage,output_tokens}')::numeric, 0) AS output_tokens,
           COALESCE((call.value #>> '{usage,cached_input_tokens}')::numeric, 0)
               AS cached_input_tokens,
           COALESCE((call.value #>> '{usage,cached_input_audio_tokens}')::numeric, 0)
               AS cached_input_audio_tokens,
           COALESCE((call.value #>> '{usage,input_audio_tokens}')::numeric, 0)
               AS input_audio_tokens,
           COALESCE((call.value #>> '{usage,output_audio_tokens}')::numeric, 0)
               AS output_audio_tokens
    FROM conversation_items AS item
    CROSS JOIN LATERAL jsonb_array_elements(item.payload #> '{metrics,calls}')
        WITH ORDINALITY AS call(value, ordinality)
    WHERE item.item_type = 'message'
      AND item.role = 'assistant'
), resolved AS (
    SELECT calls.*,
           price_card.input_rate,
           price_card.cached_input_rate,
           price_card.input_audio_rate,
           price_card.output_rate,
           price_card.output_audio_rate,
           price_card.currency,
           CASE
               WHEN calls.model = 'gpt-5.4'
                    AND calls.input_tokens >= 272001 THEN 4.387889::numeric
               ELSE price_card.input_rate
           END AS effective_input_rate,
           CASE
               WHEN calls.model = 'gpt-5.4'
                    AND calls.input_tokens >= 272001 THEN 0.438789::numeric
               ELSE price_card.cached_input_rate
           END AS effective_cached_input_rate,
           CASE
               WHEN calls.model = 'gpt-5.4'
                    AND calls.input_tokens >= 272001 THEN 19.745502::numeric
               ELSE price_card.output_rate
           END AS effective_output_rate
    FROM calls
    JOIN price_card
      ON price_card.model = CASE
          WHEN calls.model = 'gpt-5-mini' THEN 'gpt-5-mini'
          WHEN calls.model = 'gpt-5.4-mini' OR calls.model LIKE 'gpt-5.4-mini-%'
              THEN 'gpt-5.4-mini'
          WHEN calls.model = 'gpt-5.6-luna' THEN 'gpt-5.6-luna'
          WHEN calls.model = 'gpt-5.4' OR calls.model LIKE 'gpt-5.4-%'
              THEN 'gpt-5.4'
          WHEN calls.model LIKE 'gpt-5-mini-%' THEN 'gpt-5-mini'
          WHEN calls.model LIKE 'gemini-%' THEN 'gemini-3.1-flash-live-preview'
          ELSE 'gpt-5.6-luna'
      END
), updated_items AS (
    SELECT item_id,
           jsonb_agg(
               CASE
                   WHEN raw_call #>> '{cost,amount}' IS NULL
                     OR raw_call #>> '{cost,currency}' IS NULL
                   THEN jsonb_set(
                       raw_call,
                       '{cost}',
                       jsonb_build_object(
                           'amount', ROUND((
                               (input_tokens - cached_input_tokens
                                   - (input_audio_tokens - cached_input_audio_tokens))
                                   * effective_input_rate
                               + (cached_input_tokens - cached_input_audio_tokens)
                                   * COALESCE(effective_cached_input_rate, effective_input_rate)
                               + cached_input_audio_tokens
                                   * COALESCE(effective_cached_input_rate,
                                              effective_input_rate)
                               + (input_audio_tokens - cached_input_audio_tokens)
                                   * COALESCE(input_audio_rate, effective_input_rate)
                               + (output_tokens - output_audio_tokens) * effective_output_rate
                               + output_audio_tokens
                                   * COALESCE(output_audio_rate, effective_output_rate)
                           ) / 1000000, 12),
                           'currency', currency
                       ),
                       false
                   )
                   ELSE raw_call
               END
               ORDER BY ordinality
           ) AS calls
    FROM resolved
    GROUP BY item_id
)
UPDATE conversation_items AS item
SET payload = jsonb_set(item.payload, '{metrics,calls}', updated_items.calls, false)
FROM updated_items
WHERE item.id = updated_items.item_id;
