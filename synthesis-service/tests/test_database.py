from pathlib import Path
import tempfile
from unittest.mock import patch

import psycopg

from app import database
from tests.support import SynthesisTestCase, statements


class DatabaseTests(SynthesisTestCase):
    def migrations(self, folder):
        directory = Path(folder) / "migrations"
        directory.mkdir()
        for name in ("0004_snapshots_citations.sql", "0005_service_heartbeats.sql", "0006_extractive_results.sql"):
            (directory / name).write_text(f"BEGIN;\nSELECT '{name}';\nCOMMIT;\n", encoding="utf-8")

    def test_missing_required_migrations_fail_before_connecting(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(database, "__file__", str(Path(folder) / "app/database.py")), patch.object(database, "get_connection") as connect:
            with self.assertRaises(RuntimeError):
                database.init_db()
            connect.assert_not_called()

    def test_initialization_serializes_and_applies_migrations_in_one_transaction(self):
        connection, cursor = self.database(database)
        with tempfile.TemporaryDirectory() as folder:
            self.migrations(folder)
            with patch.object(database, "__file__", str(Path(folder) / "app/database.py")):
                database.init_db()
        queries = statements(cursor)
        self.assertIn("pg_advisory_xact_lock", queries[0])
        self.assertEqual([s for s in queries if "snapshots_citations.sql" in s or "service_heartbeats.sql" in s or "extractive_results.sql" in s], [
            "SELECT '0004_snapshots_citations.sql';", "SELECT '0005_service_heartbeats.sql';", "SELECT '0006_extractive_results.sql';"])
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_migration_error_rolls_back_initialization(self):
        connection, cursor = self.database(database)
        def execute(sql, *args):
            if "SELECT '0005" in sql:
                raise psycopg.ProgrammingError("bad migration")
        cursor.execute.side_effect = execute
        with tempfile.TemporaryDirectory() as folder:
            self.migrations(folder)
            with patch.object(database, "__file__", str(Path(folder) / "app/database.py")), self.assertRaises(psycopg.ProgrammingError):
                database.init_db()
        self.assertIs(connection.__exit__.call_args.args[0], psycopg.ProgrammingError)
        self.assertFalse(any("0006" in s for s in statements(cursor)))
