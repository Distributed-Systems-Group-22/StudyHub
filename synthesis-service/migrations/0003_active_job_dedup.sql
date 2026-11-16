-- Only one active job may exist for the same user
-- and version-aware cache key.
--
-- Completed and failed jobs are excluded so users
-- can regenerate summaries when needed.

CREATE UNIQUE INDEX IF NOT EXISTS
    idx_synthesis_one_active_job_per_cache
ON synthesis_jobs (
    user_id,
    cache_key
)
WHERE status IN (
    'PENDING',
    'RUNNING'
);
