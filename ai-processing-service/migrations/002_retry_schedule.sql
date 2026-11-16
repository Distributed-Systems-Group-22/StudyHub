-- Keep outage retries separate from permanent processing failures.
-- Idempotent ALTER statements also support databases initialized by version 1.
ALTER TABLE ai_processing.jobs
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ai_processing.jobs
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
