"""Hard process termination, fresh imports and actual durable host stores."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.onboarding_crash_worker import OLD_PASSWORD, PASSWORD

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 with pinned Hermes")
class OnboardingProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".onboarding-recovery-", dir=ROOT.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ".hermes" / "profiles" / "beta"
        self.outputs = []

    def worker(self, point, *, rotation=False):
        command = [sys.executable, "-m", "tests.onboarding_crash_worker", str(self.root), point]
        if rotation:
            command.append("--rotation")
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.outputs.extend([result.stdout, result.stderr])
        self.assertEqual(result.returncode, 0 if point == "resume" else 77, result.stdout + result.stderr)
        return json.loads(result.stdout) if point == "resume" else None

    def crash_at(self, point, *, rotation=False):
        self.worker(point, rotation=rotation)
        self.assertFalse((self.root / "worker-finally-ran").exists(), "os._exit must bypass finally")
        self.assertEqual(json.loads((self.root / "crash-evidence.json").read_text()), {"boundary": point})

    def deliveries(self):
        path = self.home / "test-deliveries.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def audit(self):
        for password in (PASSWORD, OLD_PASSWORD):
            self.assertNotIn(password, "".join(self.outputs))
            for path in self.root.rglob("*"):
                if path.is_file() and path not in (self.home / ".env", self.root / ".hermes" / ".env"):
                    self.assertNotIn(password.encode(), path.read_bytes(), str(path))

    def test_ambiguous_password_rotation_does_not_reuse_old_password_as_success(self):
        self.crash_at("before_credential_write", rotation=True)
        self.assertIn(OLD_PASSWORD, (self.home / ".env").read_text())
        result = self.worker("resume")
        self.assertEqual(result["state"], "needs_secret")
        self.assertEqual(result["cursor"], [7, 100])
        self.assertEqual(self.deliveries(), [])
        self.assertTrue(result["lock_acquired"])
        self.audit()

    def test_written_rotation_without_verified_marker_still_requires_new_secret(self):
        self.crash_at("credential_written_before_verified_marker", rotation=True)
        self.assertIn(PASSWORD, (self.home / ".env").read_text())
        self.assertNotIn(OLD_PASSWORD, (self.home / ".env").read_text())
        result = self.worker("resume")
        self.assertEqual(result["state"], "needs_secret")
        self.assertEqual(result["cursor"], [7, 100])
        self.assertEqual(self.deliveries(), [])
        self.audit()

    def test_after_settings_publish_recovers_without_secret_or_uid_reset(self):
        self.crash_at("settings_written_before_marker")
        result = self.worker("resume")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["cron_count"], 1)
        self.assertEqual(result["cursor"], [7, 100])
        self.assertEqual(result["pending_secrets"], 0)
        self.assertEqual(len(self.deliveries()), 1)
        self.audit()

    def test_recovery_assertion_rejects_orphan_cursor_and_disabled_active_settings(self):
        from mail_notifier.config import secret_name
        from ruamel.yaml import YAML
        self.crash_at("completed")
        yaml = YAML()
        path = self.home / "config.yaml"
        data = yaml.load(path)
        account = data["plugins"]["entries"]["hermes-mail-notifier"]["settings"]["configuration"]["accounts"][0]
        original_id = account["id"]
        for changes in ({"id": "orphanreplacement"}, {"id": original_id, "enabled": False}):
            with self.subTest(changes=changes):
                account.update(changes)
                account["password_secret"] = secret_name(account["id"])
                yaml.dump(data, path)
                with self.assertRaisesRegex(AssertionError, "resumed settings changed account identity/enabled state"):
                    self.worker("resume")

    def test_each_publication_boundary_reopens_files_releases_locks_and_only_ambiguous_send_repeats(self):
        points = (
            ("validated_before_confirmation", "needs_secret", None),
            ("before_credential_write", "needs_secret", None),
            ("credential_written_before_verified_marker", "needs_secret", None),
            ("credential_verified_before_baseline_intent", "completed", [7, 900]),
            ("before_baseline_intent", "completed", [7, 900]),
            ("baseline_intent_written_before_initialization", "completed", [7, 100]),
            ("before_baseline_initialization", "completed", [7, 100]),
            ("baseline_initialized_before_settings", "completed", [7, 100]),
            ("before_settings_publication", "completed", [7, 100]),
            ("settings_written_before_marker", "completed", [7, 100]),
            ("settings_marker_written", "completed", [7, 100]),
            ("before_cron_write", "completed", [7, 100]),
            ("cron_written_before_marker", "completed", [7, 100]),
            ("cron_marker_written", "completed", [7, 100]),
            ("before_test_send", "completed", [7, 100]),
            ("test_sent_before_marker", "completed", [7, 100]),
            ("test_recorded_before_completion", "completed", [7, 100]),
            ("completed", "completed", [7, 100]),
        )
        for point, expected, cursor in points:
            with self.subTest(point=point):
                # Each boundary gets independent files, never a previous result.
                with tempfile.TemporaryDirectory(prefix=".onboarding-boundary-", dir=ROOT.parent) as directory:
                    self.root = Path(directory)
                    self.home = self.root / ".hermes" / "profiles" / "beta"
                    self.crash_at(point)
                    before = self.deliveries()
                    for _ in range(2):
                        result = self.worker("resume")
                        self.assertEqual(result["state"], expected)
                        self.assertTrue(result["lock_acquired"])
                        self.assertEqual(result["pending_secrets"], 0)
                        self.assertEqual(result["setup_version"], 2)
                        self.assertEqual(result["cursor"], cursor)
                    self.assertEqual(result["cron_count"], 0 if expected == "needs_secret" else 1)
                    deliveries = self.deliveries()
                    self.assertEqual(len(deliveries), 0 if expected == "needs_secret" else 2 if point == "test_sent_before_marker" else 1)
                    if deliveries:
                        attempt = json.loads((self.root / "attempt.json").read_text())["draft_id"]
                        self.assertTrue(all(row["text"].endswith(attempt) for row in deliveries))
                        self.assertEqual(len({row["text"] for row in deliveries}), 1)
                    if before and point != "test_sent_before_marker":
                        self.assertEqual(deliveries, before)
                    with closing(sqlite3.connect(self.home / "plugin-data/hermes-mail-notifier/setup.db")) as db:
                        attempt = json.loads((self.root / "attempt.json").read_text())["draft_id"]
                        self.assertEqual(db.execute("SELECT state FROM drafts WHERE id=?", (attempt,)).fetchall(), [(expected,)])
                    self.audit()
