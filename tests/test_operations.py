import tempfile
import time
import unittest
from dataclasses import replace

from mail_notifier.config import Account, ConfigError
from mail_notifier.config import settings_fingerprint
from mail_notifier.hermes_host import HostError
from mail_notifier.message import MailContent
from mail_notifier.migrations import mail_id
from mail_notifier.service import check_mail
from tests.support import account_data, classification_data, feature
from tests.test_cli import SetupHost
from tests.test_service import FakeMailbox


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.operations")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SetupHost(self.temp.name)
        self.accounts = (Account(**account_data()), Account(**account_data("b2")))
        self.host.settings = replace(self.host.settings, accounts=self.accounts)
        self.host.passwords = {a.id: "test-only" for a in self.accounts}
        self.store = self.host.open_store()
        self.addCleanup(self.store.close)
        for account in self.accounts:
            self.store.initialize(account, 7, 100)
            key = (account.id, "INBOX", 7, 101)
            self.store.enqueue(account, 7, [101])
            self.store.cache_message(key, MailContent("person@example.com", "", "private subject", "", "private body", False, False), 1000)
            self.store.save_decision(key, classification_data(), notification="stored summary", target="telegram:111",
                                     fingerprint="test", now=1000)
        self.key = ("a1", "INBOX", 7, 101)
        self.identifier = mail_id(self.key)

    def test_retry_preserves_decision_and_does_not_call_model(self):
        self.store.retry(self.key, "delivery_failed", 1000, permanent=True)
        self.module.queue(self.host, action="retry", mail_id=self.identifier)
        before = self.store.get_work(self.identifier).decision
        report = check_mail(self.host, mailbox_factory=lambda *a, **kw: (_ for _ in ()).throw(HostError("credential_unavailable")))
        self.assertEqual(report.sent, 2)
        self.assertEqual(self.host.model_calls, 0)
        self.assertEqual(self.host.deliveries[0], ("telegram:111", "stored summary"))
        self.assertEqual(self.store.get_work(self.identifier).decision, before)
        with self.assertRaises(HostError):
            self.module.queue(self.host, action="retry", mail_id=self.identifier)

    def test_one_retry_expires_old_body_and_clears_its_source_hold(self):
        key = ("a1", "INBOX", 7, 102)
        self.store.enqueue(self.accounts[0], 7, [102])
        self.store.cache_message(key, MailContent("person@example.com", "", "new", "", "body", False, False), time.time() - 8 * 86400)
        self.store.set_meta("source_hold:a1", {"code": "imap_authentication_failed", "fingerprint": settings_fingerprint(self.host.settings)})
        self.module.queue(self.host, action="retry", mail_id=mail_id(key))
        box = FakeMailbox(self.accounts[0])
        box.add(102)
        check_mail(self.host, mailbox_factory=lambda account, password, **kw: box if account.id == "a1" else FakeMailbox(account))
        self.assertEqual(self.store.get_work(mail_id(key)).status, "sent")
        self.assertEqual(box.fetches, 1)

    def test_queue_scope_target_and_lock_validation_precedes_mutation(self):
        for kwargs in ({"action": "retry"}, {"action": "retarget", "mail_id": self.identifier, "target": "discord"},
                       {"action": "retry", "mail_id": self.identifier, "account_id": "b2"},
                       {"action": "retry", "account_id": "unknown"}):
            with self.assertRaises(HostError):
                self.module.queue(self.host, **kwargs)
        with self.store.run_lock(), self.assertRaises(HostError):
            self.module.queue(self.host, action="retarget", account_id="a1", target="telegram:222")
        self.assertEqual(self.store.get_work(self.identifier).target, "telegram:111")
        self.module.queue(self.host, action="retarget", account_id="a1", target="telegram:222")
        self.assertEqual(self.store.get_work(self.identifier).target, "telegram:222")
        self.assertEqual(self.store.get_work(mail_id(("b2", "INBOX", 7, 101))).target, "telegram:111")
        key = ("a1", "INBOX", 7, 102)
        self.store.enqueue(self.accounts[0], 7, [102])
        with self.assertRaises(HostError):
            self.module.queue(self.host, action="retarget", mail_id=mail_id(key), target="telegram:222")
        self.assertNotIn("private", str(self.module.queue(self.host)))

    def test_policy_updates_preserve_settings_and_report_conflicts(self):
        old = self.host.settings
        for changes in ({"target": "telegram:222"}, {"ignored_senders": ["bad"]}):
            with self.assertRaises((HostError, ConfigError)):
                self.module.policy(self.host, changes=changes)
            self.assertEqual(self.host.settings, old)
        result = self.module.policy(self.host, changes={"importance_instructions": "personal mail",
                                    "ignored_senders": ["@example.com"], "always_notify_senders": ["person@example.com"]})
        self.assertEqual(self.host.settings.accounts, old.accounts)
        self.assertEqual(self.host.settings.target, old.target)
        self.assertEqual(self.host.settings.max_fetches, old.max_fetches)
        self.assertTrue(result["conflicts"])

    def test_enable_disable_preserve_work_and_uid_and_failed_enable_is_atomic(self):
        self.module.set_account_enabled(self.host, "a1", False)
        self.assertFalse(self.host.settings.accounts[0].enabled)
        self.assertEqual(self.store.get_work(self.identifier).status, "ready")
        with self.assertRaises(HostError):
            self.module.set_account_enabled(self.host, "a1", True,
                mailbox_factory=lambda *a: (_ for _ in ()).throw(HostError("credential_unavailable")))
        self.assertFalse(self.host.settings.accounts[0].enabled)
        box = FakeMailbox(self.accounts[0])
        box.highest = 200
        self.module.set_account_enabled(self.host, "a1", True, mailbox_factory=lambda *a: box)
        self.assertTrue(self.host.settings.accounts[0].enabled)
        self.assertEqual(self.store.cursor("a1"), (7, 101))

    def test_explicit_history_and_feedback_roundtrip(self):
        record = self.module.history(self.host, mail_id=self.identifier)["items"][0]
        self.assertEqual(record["decision"]["mail"]["subject"], "private subject")
        row = self.module.feedback(self.host, action="add", mail_id=self.identifier, decision="suppress", note="promo")["item"]
        self.assertEqual(self.module.feedback(self.host, action="list")["items"][0]["note"], "promo")
        self.module.feedback(self.host, action="remove", feedback_id=row["id"])
        self.assertEqual(self.module.feedback(self.host, action="list")["items"], [])
