import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mail_notifier.config import Account
from tests.support import account_data, feature


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.store")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.account = Account(**account_data())
        self.key = ("a1", "INBOX", 7, 101)
        self.open_store()
        self.addCleanup(lambda: self.store.close())
        self.store.initialize(self.account, 7, 100)

    def open_store(self):
        self.connection = sqlite3.connect(self.path / "state.db")
        self.store = self.module.Store(self.connection, self.path / "run.lock")

    def test_checkpoint_and_enqueue_atomic_on_failure(self):
        self.connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON work WHEN NEW.uid = 102 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.enqueue(self.account, 7, [101, 102])
        self.assertEqual(self.store.cursor("a1"), (7, 100))
        self.assertEqual(self.store.due("a1", 0, 20), [])

    def test_unique_keys_and_old_uids_do_not_reappear(self):
        other = Account(**account_data("b2"))
        self.store.initialize(other, 7, 100)
        self.store.enqueue(self.account, 7, [99, 101, 101])
        self.store.enqueue(other, 7, [101])
        self.assertEqual(len(self.store.due("a1", 0, 20)), 1)
        self.assertEqual(len(self.store.due("b2", 0, 20)), 1)
        self.store.finish(self.key, "ignored", 10)
        self.store.prune(40 * 86400)
        self.store.enqueue(self.account, 7, [101])
        self.assertEqual(self.store.due("a1", 40 * 86400, 20), [])

    def test_restart_preserves_payload_and_original_baseline(self):
        self.store.enqueue(self.account, 7, [101])
        self.store.prepare(self.key, "saved summary")
        self.store.set_meta("cron_job_id", "job123")
        self.store.close()
        self.open_store()
        self.store.initialize(self.account, 7, 999)
        self.assertEqual(self.store.cursor("a1"), (7, 101))
        self.assertEqual(self.store.due("a1", 0, 20)[0].notification, "saved summary")
        self.assertEqual(self.store.get_meta("cron_job_id"), "job123")
        self.assertNotIn("saved summary", json.dumps(self.store.status()))

    def test_retry_backoff_is_bounded_and_ready_stays_ready(self):
        self.store.enqueue(self.account, 7, [101])
        self.store.prepare(self.key, "saved summary")
        now = 1000
        for attempts, delay in enumerate([300, 600, 1200, 2400, 3600, 3600], start=1):
            self.store.retry(self.key, "delivery_failed", now)
            self.assertEqual(self.store.due("a1", now + delay - 1, 20), [])
            item = self.store.due("a1", now + delay, 20)[0]
            self.assertEqual((item.status, item.attempts, item.retry_at), ("ready", attempts, now + delay))
            now += delay

    def test_epoch_change_blocks_until_explicit_reset(self):
        self.store.enqueue(self.account, 7, [101])
        with self.assertRaises(self.module.UIDValidityChanged):
            self.store.enqueue(self.account, 8, [102])
        self.assertEqual(self.store.cursor("a1"), (7, 101))
        other = Account(**account_data("b2"))
        self.store.initialize(other, 7, 0)
        self.store.enqueue(other, 7, [1])
        self.assertEqual(self.store.reset(self.account, 8, 120, 1000), 1)
        self.assertEqual(self.store.cursor("a1"), (8, 120))
        self.assertEqual(self.store.due("a1", 1000, 20), [])
        self.assertEqual(len(self.store.due("b2", 1000, 20)), 1)

    def test_prune_keeps_pending_cursor_and_recent_completed(self):
        self.store.enqueue(self.account, 7, [101, 102, 103])
        self.store.finish(self.key, "sent", 10, {"message_id": "123"})
        self.store.finish(("a1", "INBOX", 7, 102), "missing", 35 * 86400)
        self.assertEqual(self.store.prune(40 * 86400), 1)
        self.assertEqual(self.store.cursor("a1"), (7, 103))
        self.assertEqual([w.key[-1] for w in self.store.due("a1", 40 * 86400, 20)], [103])

    def test_errors_and_status_exclude_message_payloads(self):
        self.store.record_error("a1", "imap_connection_failed", 123)
        status = self.store.status()
        self.assertEqual(status["accounts"][0]["last_error"], "imap_connection_failed")
        self.store.record_error("a1", None, 124)
        self.assertIsNone(self.store.status()["accounts"][0]["last_error"])

    def test_process_lock_is_nonblocking_and_released_after_exception(self):
        probe = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        with self.assertRaisesRegex(RuntimeError, "test abort"):
            with self.store.run_lock() as acquired:
                self.assertTrue(acquired)
                result = subprocess.run([sys.executable, "-c", probe, str(self.path / "run.lock")], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                with self.store.run_lock() as second:
                    self.assertFalse(second)
                raise RuntimeError("test abort")
        result = subprocess.run([sys.executable, "-c", probe, str(self.path / "run.lock")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
