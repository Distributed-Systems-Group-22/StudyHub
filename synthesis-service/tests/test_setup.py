import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class SetupTests(unittest.TestCase):
    def test_setup_preserves_credentials_adds_missing_defaults_and_is_idempotent(self):
        root = Path(__file__).resolve().parents[2]
        shell_python = (root / "setup/setup-content-service.sh").read_text(encoding="utf-8").split("<<'PYTHON'\n", 1)[1].split("\nPYTHON", 1)[0]
        with tempfile.TemporaryDirectory(prefix="synthesis-unit-setup-") as scratch:
            folder = Path(scratch)
            (folder / "setup").mkdir()
            shutil.copy(root / ".env.example", folder / ".env.example")
            script = folder / "setup/setup-content-service.ps1"
            shutil.copy(root / "setup/setup-content-service.ps1", script)
            commands = [[sys.executable, "-c", shell_python]]
            if os.name == "nt":
                commands.append(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
            for command in commands:
                with self.subTest(command=command[0]):
                    file = folder / ".env"
                    file.write_text("POSTGRES_PASSWORD=keep_existing\nSTUDYHUB_PROCESSING_TOKEN=keep_token\nSTUDYHUB_SEARCH_TOKEN=\n", encoding="utf-8")
                    subprocess.run(command, cwd=folder, capture_output=True, check=True)
                    first = file.read_text(encoding="utf-8")
                    self.assertIn("POSTGRES_PASSWORD=keep_existing", first)
                    self.assertIn("STUDYHUB_PROCESSING_TOKEN=keep_token", first)
                    self.assertIn("DOCUMENT_PROVIDER=mock", first)
                    self.assertIn("AI_SUMMARY_MODEL=qwen3:4b", first)
                    self.assertNotIn("STUDYHUB_SEARCH_TOKEN=\n", first)
                    subprocess.run(command, cwd=folder, capture_output=True, check=True)
                    self.assertEqual(file.read_text(encoding="utf-8"), first)
