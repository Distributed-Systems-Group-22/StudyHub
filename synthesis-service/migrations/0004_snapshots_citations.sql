-- Nullable snapshots deliberately distinguish legacy jobs from verified requests.
ALTER TABLE synthesis_jobs ADD COLUMN IF NOT EXISTS document_versions JSONB;
ALTER TABLE synthesis_jobs ADD COLUMN IF NOT EXISTS generation_config JSONB;
ALTER TABLE synthesis_results ADD COLUMN IF NOT EXISTS citations JSONB;
ALTER TABLE summary_sources ADD COLUMN IF NOT EXISTS source_location TEXT;

-- A deletion delivered before result insertion must still prevent completion.
CREATE TABLE IF NOT EXISTS synthesis_deleted_documents (
    document_id TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

UPDATE synthesis_jobs
SET status = 'FAILED', claim_token = NULL, lease_until = NULL,
    error_message = 'legacy_snapshot_missing: resubmit request', updated_at = NOW()
WHERE status IN ('PENDING', 'RUNNING')
  AND (document_versions IS NULL OR generation_config IS NULL);

UPDATE synthesis_results r SET status = 'INVALIDATED'
FROM synthesis_jobs j
WHERE r.job_id = j.job_id AND r.status = 'VALID'
  AND (j.document_versions IS NULL OR j.generation_config IS NULL);
