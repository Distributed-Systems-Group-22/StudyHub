
import os
from pathlib import Path

import psycopg


DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg.connect(DATABASE_URL, connect_timeout=5,
                           options="-c statement_timeout=10000 -c lock_timeout=5000")


def init_db():
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not (migrations_dir / "0004_snapshots_citations.sql").is_file():
        raise RuntimeError("Synthesis migrations are missing; refusing an incomplete schema")
    with get_connection() as conn:
        with conn.cursor() as cur:

            # 1. Background jobs
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synthesis_jobs (
                    job_id UUID PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN (
                            'PENDING',
                            'RUNNING',
                            'COMPLETED',
                            'FAILED'
                        )),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # 2. Summary results
            cur.execute("""
                CREATE TABLE IF NOT EXISTS synthesis_results (
                    summary_id UUID PRIMARY KEY,
                    job_id UUID REFERENCES synthesis_jobs(job_id),
                    cache_key TEXT NOT NULL,
                    summary_text TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'VALID'
                        CHECK (status IN (
                            'VALID',
                            'INVALIDATED'
                        )),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # 3. Source references
            cur.execute("""
                CREATE TABLE IF NOT EXISTS summary_sources (
                    id BIGSERIAL PRIMARY KEY,
                    summary_id UUID NOT NULL
                        REFERENCES synthesis_results(summary_id)
                        ON DELETE CASCADE,
                    document_id TEXT NOT NULL,
                    document_version INTEGER NOT NULL,
                    chunk_id TEXT,
                    page INTEGER
                )
            """)

            # Indexes
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_user
                ON synthesis_jobs(user_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_results_cache
                ON synthesis_results(cache_key)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sources_document
                ON summary_sources(document_id)
            """)

            # Existing migrations contain transaction wrappers; the startup
            # connection owns the transaction so schema changes commit together.
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                sql = "\n".join(
                    line for line in sql_file.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip().upper() not in ("BEGIN;", "COMMIT;")
                )
                cur.execute(sql)

    print("Database tables and migrations initialised.", flush=True)
