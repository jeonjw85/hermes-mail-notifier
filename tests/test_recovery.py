import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProcessRecoveryTests(unittest.TestCase):
    def test_six_crash_boundaries_resume_and_only_ambiguous_send_repeats(self):
        worker = Path(__file__).with_name("crash_worker.py")
        self.assertTrue(worker.exists(), "crash recovery worker missing")
        for point in ("before_cache", "after_cache", "before_decision", "after_decision", "after_send", "after_record"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as path:
                crashed = subprocess.run([sys.executable, "-m", "tests.crash_worker", path, point], capture_output=True, text=True)
                self.assertEqual(crashed.returncode, 77, crashed.stderr)
                for _ in range(2):
                    resumed = subprocess.run([sys.executable, "-m", "tests.crash_worker", path, "resume"], capture_output=True, text=True)
                    self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
                lines = (Path(path) / "transport.jsonl").read_text().splitlines()
                deliveries = [json.loads(line) for line in lines]
                self.assertEqual(len(deliveries), 2 if point == "after_send" else 1)
                self.assertEqual(len({row["text"] for row in deliveries}), 1)
                self.assertIn("메일 ID:", deliveries[0]["text"])
                db = sqlite3.connect(Path(path) / "state.db")
                try:
                    self.assertEqual(db.execute("SELECT status FROM work").fetchall(), [("sent",)])
                finally:
                    db.close()
