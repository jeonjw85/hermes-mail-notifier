import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mail_notifier.config import Account, Settings
from mail_notifier.message import MailContent
from mail_notifier.migrations import mail_id
from mail_notifier.pipeline import classify_one
from mail_notifier.store import Store, StorageError
from tests.support import account_data, classification_data


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(sqlite3.connect(":memory:"), Path(self.temp.name) / "lock")
        self.addCleanup(self.store.close)
        self.account = Account(**account_data())
        self.store.initialize(self.account, 7, 0)

    def decision(self, uid, sender="person@example.com"):
        key = (self.account.id, "INBOX", 7, uid)
        self.store.enqueue(self.account, 7, [uid], now=uid)
        self.store.cache_message(key, MailContent(sender, "Person", f"subject {uid}", "", "body", False, False), uid)
        self.store.save_decision(key, classification_data(decision="suppress"), notification=None,
                                 target="telegram:123", fingerprint="test", now=uid)
        return mail_id(key)

    def test_feedback_updates_without_rewriting_original_decision(self):
        identifier = self.decision(1)
        before = self.store.get_work(identifier).decision
        self.assertTrue(hasattr(self.store, "add_feedback"), "feedback persistence missing")
        first = self.store.add_feedback(identifier, "notify", "personal request", 100)
        updated = self.store.add_feedback(identifier, "review", "uncertain", 101)
        self.assertEqual(first["id"], updated["id"])
        self.assertEqual(len(self.store.list_feedback()), 1)
        self.assertEqual(self.store.get_work(identifier).decision, before)
        self.assertTrue(self.store.remove_feedback(first["id"]))
        self.assertEqual(self.store.list_feedback(), [])

    def test_retention_keeps_latest_200_for_180_days_independent_of_history(self):
        self.assertTrue(hasattr(self.store, "add_feedback"), "feedback persistence missing")
        for uid in range(1, 202):
            self.store.add_feedback(self.decision(uid), "notify", "correction", uid)
        rows = self.store.list_feedback(200)
        self.assertEqual(len(rows), 200)
        self.assertEqual(min(row["created_at"] for row in rows), 2)
        self.store.prune(31 * 86400)
        self.assertEqual(self.store.history(), [])
        self.assertEqual(len(self.store.feedback_for("person@example.com")), 5)
        self.store.prune(180 * 86400 + 3)
        self.assertEqual(len(self.store.list_feedback(200)), 199)

    def test_classification_uses_only_five_matching_nonempty_senders(self):
        self.assertTrue(hasattr(self.store, "add_feedback"), "feedback persistence missing")
        for uid in range(1, 8):
            self.store.add_feedback(self.decision(uid), "notify", f"note {uid}", uid)
        for uid, sender in ((8, "other@example.com"), (9, ""), (10, "invalid")):
            self.store.add_feedback(self.decision(uid, sender), "notify", "unrelated", uid)
        self.assertEqual(self.store.feedback_for(""), [])
        self.assertEqual(self.store.feedback_for("invalid"), [])
        self.assertEqual(len(self.store.feedback_for("PERSON@example.com")), 5)
        key = (self.account.id, "INBOX", 7, 11)
        self.store.enqueue(self.account, 7, [11])
        self.store.cache_message(key, MailContent("person@example.com", "", "new", "", "body", False, False), 20)
        captured = []
        def complete(payload):
            captured.append(json.loads(payload["input"][0]["text"]))
            return classification_data(decision="suppress")
        result = classify_one(self.store, self.store.get_work(mail_id(key)), self.account, Settings(), complete, 30)
        self.assertEqual(result.decision, "suppress")
        self.assertEqual([row["note"] for row in captured[0]["feedback"]], [f"note {i}" for i in range(3, 8)])

    def test_invalid_feedback_is_rejected_before_write(self):
        identifier = self.decision(1)
        self.assertTrue(hasattr(self.store, "add_feedback"), "feedback persistence missing")
        for target, decision, note in (("missing", "notify", ""), (identifier, "discard", ""),
                                       (identifier, "notify", "x" * 1001)):
            with self.assertRaises(StorageError):
                self.store.add_feedback(target, decision, note, 100)
        self.assertEqual(self.store.list_feedback(), [])
