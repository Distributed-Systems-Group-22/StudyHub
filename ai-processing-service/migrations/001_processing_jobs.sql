CREATE SCHEMA IF NOT EXISTS ai_processing;
CREATE TABLE IF NOT EXISTS ai_processing.jobs (
    document_id UUID NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version > 0),
    event_id UUID NOT NULL,
    result_event_id UUID NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'result_ready', 'failed', 'completed', 'skipped')),
    result JSONB,
    error_type TEXT,
    PRIMARY KEY (document_id, document_version)
);
