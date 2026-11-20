-- Keep old rows for audit, but do not serve unverified generated prose as grounded excerpts.
UPDATE synthesis_results SET status = 'INVALIDATED'
WHERE status = 'VALID' AND prompt_version <> 'synthesis-extractive-v3';

UPDATE synthesis_jobs
SET status = 'FAILED', claim_token = NULL, lease_until = NULL,
    error_message = 'generation_config_changed: extractive upgrade; resubmit', updated_at = NOW()
WHERE status IN ('PENDING', 'RUNNING')
  AND generation_config->>'prompt_version' IS DISTINCT FROM 'synthesis-extractive-v3';
