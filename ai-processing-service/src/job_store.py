"""Service-owned schema; session advisory lock serializes a document/version.
No shared Content tables are read or modified.
"""
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

class JobStore:
    """Create owned tables and coordinate consumers processing the same version."""
    def __init__(self, database_url):
        self.url = database_url

    def initialize(self):
        """Apply service-owned, repeatable SQL updates in one transaction."""
        migrations = Path(__file__).resolve().parents[1] / 'migrations'
        with psycopg.connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(4172022)")
            # Serialize idempotent initialization across concurrently starting workers.
            for migration in sorted(migrations.glob('*.sql')):
                conn.execute(migration.read_text())

    @contextmanager
    def lock(self, event):
        """Hold a session lock until delivery processing has left the context."""
        doc, version = event['documentId'], event['documentVersion']
        with psycopg.connect(self.url, autocommit=True, row_factory=dict_row) as conn:
            key = f'{doc}:{version}'
            acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked',
                                    (key,)).fetchone()['locked']
            if not acquired:
                yield None
                return
            try:
                conn.execute("""INSERT INTO ai_processing.jobs
                    (document_id, document_version, event_id, result_event_id)
                    VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (doc, version, event['eventId'], uuid4()))
                yield Job(conn, doc, version)
            finally:
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', (key,))

class Job:
    """Persist transitions using the connection that holds the advisory lock."""
    def __init__(self, conn, doc, version):
        self.conn, self.doc, self.version = conn, doc, version

    def read(self):
        """Read the latest state while the caller holds the version lock."""
        return self.conn.execute('SELECT * FROM ai_processing.jobs WHERE document_id=%s AND document_version=%s',
                                 (self.doc, self.version)).fetchone()

    def attempt(self):
        """Reserve an attempt before external work so crashes cannot loop forever."""
        self.conn.execute('UPDATE ai_processing.jobs SET attempts=attempts+1 WHERE document_id=%s AND document_version=%s',
                          (self.doc, self.version))

    def defer(self, delay, error_type):
        """Persist cooldown and refund the attempt used by a temporary outage."""
        self.conn.execute("""UPDATE ai_processing.jobs
            SET attempts=GREATEST(attempts-1, 0), retry_count=retry_count+1,
                next_attempt_at=NOW() + (%s * INTERVAL '1 second'), error_type=%s
            WHERE document_id=%s AND document_version=%s""",
            (delay, error_type, self.doc, self.version))

    def save(self, state, result=None, error_type=None):
        """Commit one state transition; clear staged output only when requested."""
        self.conn.execute("""UPDATE ai_processing.jobs SET state=%s, result=%s, error_type=%s, next_attempt_at=NULL
                            WHERE document_id=%s AND document_version=%s""",
                          (state, Jsonb(result) if result is not None else None,
                           error_type, self.doc, self.version))
