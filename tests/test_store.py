import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mail_notifier.config import Account
from mail_notifier.message import MailContent
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

    def cache(self, uid=101, now=1000):
        self.assertTrue(hasattr(self.store, "cache_message"), "durable input cache is not implemented")
        self.store.enqueue(self.account, 7, [uid])
        key = ("a1", "INBOX", 7, uid)
        self.store.cache_message(key, MailContent("boss@example.com", "Boss", "계약", "", "내일 회신", False, False), now)
        return key

    def save_decision(self, key, decision="notify", now=1001):
        result = {"category": "work", "decision": decision, "priority": "normal", "reason": "회신 필요",
                  "summary": "계약 회신", "action": "회신", "deadline": "내일", "evidence": ["내일 회신"]}
        self.store.save_decision(key, result, notification="saved summary" if decision != "suppress" else None,
                                 target="telegram:123", fingerprint="test-policy", now=now)

    def test_cache_and_decision_survive_restart_without_refetch(self):
        key = self.cache()
        item = self.store.due("a1", 1000, 1)[0]
        self.store.close()
        self.open_store()
        self.assertEqual(self.store.get_work(item.mail_id).payload["text"], "내일 회신")
        self.save_decision(key)
        self.store.close()
        self.open_store()
        restored = self.store.get_work(item.mail_id)
        self.assertIsNone(restored.payload)
        self.assertEqual((restored.status, restored.target, restored.notification), ("ready", "telegram:123", "saved summary"))
        self.assertEqual(restored.decision["reason"], "회신 필요")
        self.assertEqual(restored.decision["settings_fingerprint"], "test-policy")

    def test_status_reports_known_usage_without_inventing_missing_cost(self):
        key = self.cache()
        self.store.save_decision(key, {"decision": "suppress", "usage": {"total_tokens": 15, "cost_usd": None}},
                                 notification=None, target="telegram:123", fingerprint="test", now=1001)
        status = self.store.status()
        self.assertIn("usage", status)
        self.assertEqual(status["usage"]["total_tokens"], 15)
        self.assertIsNone(status["usage"]["cost_usd"])
        self.assertNotIn("내일 회신", json.dumps(status, ensure_ascii=False))

    def test_suppression_keeps_reason_without_delivery(self):
        key = self.cache()
        self.save_decision(key, "suppress")
        self.assertEqual(self.store.due("a1", 2000, 20), [])
        history = self.store.history()
        self.assertEqual(history[0]["status"], "suppressed")
        self.assertEqual(history[0]["decision"]["reason"], "회신 필요")
        self.assertNotIn("내일 회신", json.dumps(self.store.status(), ensure_ascii=False))

    def test_retry_tracks_each_stage_and_holds_tenth_delivery(self):
        key = self.cache()
        self.store.retry(key, "invalid_model_output", 1000)
        item = self.store.due("a1", 1300, 1)[0]
        self.assertEqual(item.attempts, 1)
        self.save_decision(key)
        for i in range(10):
            self.store.retry(key, "delivery_failed", 2000 + i * 3600)
        item = self.store.get_work(item.mail_id)
        self.assertEqual((item.status, item.resume_status, item.attempts), ("held", "ready", 10))
        self.assertEqual(self.store.due("a1", 100000, 20), [])
        row = self.store.db.execute("SELECT classify_attempts,delivery_attempts FROM work").fetchone()
        self.assertEqual(tuple(row), (1, 10))

    def test_resume_restores_stage_and_payload(self):
        key = self.cache()
        item = self.store.due("a1", 1000, 1)[0]
        self.store.retry(key, "model_unavailable", 1000, permanent=True)
        self.assertTrue(self.store.resume(item.mail_id, 2000))
        restored = self.store.due("a1", 2000, 1)[0]
        self.assertEqual((restored.status, restored.attempts), ("fetched", 0))
        self.assertEqual(restored.payload["text"], "내일 회신")

    def test_ttl_keeps_hold_and_backoff(self):
        key = self.cache(now=0)
        item = self.store.due("a1", 1000, 1)[0]
        self.store.retry(key, "model_unavailable", 8 * 86400, permanent=True)
        retry_at = self.store.get_work(item.mail_id).retry_at
        self.store.prune(8 * 86400)
        held = self.store.get_work(item.mail_id)
        self.assertEqual((held.status, held.resume_status, held.retry_at), ("held", "discovered", retry_at))
        self.assertIsNone(held.payload)
        self.assertEqual(held.attempts, 1)

    def test_queue_limit_does_not_advance_over_unstored_uid(self):
        self.assertTrue(hasattr(self.store, "capacity"), "queue capacity is not implemented")
        inserted = self.store.enqueue(self.account, 7, [101, 102, 103], max_pending=2, scanned_through=200)
        self.assertEqual(inserted, 2)
        self.assertEqual(self.store.cursor("a1"), (7, 102))
        self.assertEqual(self.store.scan_cursor("a1"), 102)
        self.assertEqual(self.store.capacity(2), 0)
        self.store.finish(self.key, "ignored", 1000)
        self.store.enqueue(self.account, 7, [103], max_pending=2, scanned_through=200)
        self.assertEqual(self.store.cursor("a1"), (7, 103))
        self.assertEqual(self.store.scan_cursor("a1"), 200)

    def test_prune_removes_only_expired_completed_content(self):
        key = self.cache()
        self.save_decision(key)
        item = self.store.due("a1", 2000, 1)[0]
        self.store.finish(key, "sent", 2000, {"message_id": "receipt"})
        self.assertEqual(self.store.get_work(item.mail_id).decision["reason"], "회신 필요")
        other = self.cache(102)
        self.save_decision(other)
        self.store.prune(40 * 86400)
        self.assertIsNone(self.store.get_work(item.mail_id))
        self.assertEqual(self.store.status()["ready"], 1)

    def test_recorded_run_is_visible_without_mail_content(self):
        self.assertTrue(hasattr(self.store, "record_run"), "run history is not implemented")
        self.store.record_run({"sent": 3, "errors": {}}, 100, 105)
        self.assertEqual(self.store.status()["last_run"]["report"]["sent"], 3)
        self.assertEqual(self.store.status()["last_run"]["finished_at"], 105)

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
